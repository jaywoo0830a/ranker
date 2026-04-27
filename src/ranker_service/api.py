"""FastAPI HTTP layer — routes implement docs/openapi.yaml.

State lives on disk (state.json per job) so the server can be restarted
without losing track of finished work. Running subprocesses, however,
do NOT survive restarts in this MVP — anything that was ``running``
when the server died will appear stuck. A future revision can scan at
startup and mark orphans as ``failed``.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import yaml
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import ValidationError

from ranker.manifest import resolve_jobs

from . import storage
from .jobs import JobManager, _now_iso
from .models import (
    ConcurrencyInfo,
    Job,
    JobConfig,
    JobListResponse,
    JobStatus,
    JobSummary,
    Progress,
)


def _config() -> tuple[Path, int]:
    jobs_root = Path(
        os.environ.get("RANKER_SERVICE_JOBS_DIR", "jobs")
    ).resolve()
    jobs_root.mkdir(parents=True, exist_ok=True)
    max_concurrent = int(
        os.environ.get("RANKER_SERVICE_MAX_CONCURRENT_JOBS", "4")
    )
    return jobs_root, max_concurrent


# Lifespan-managed singletons. App state is held on the FastAPI app
# rather than as module globals so test suites can spin up multiple
# isolated apps if needed.
@asynccontextmanager
async def lifespan(app: FastAPI):
    jobs_root, max_concurrent = _config()
    app.state.jobs_root = jobs_root
    app.state.manager = JobManager(jobs_root, max_concurrent)
    yield


app = FastAPI(
    title="Ranker Service API",
    version="0.1.0",
    lifespan=lifespan,
)


# ────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────


def _jobs_root() -> Path:
    return app.state.jobs_root


def _manager() -> JobManager:
    return app.state.manager


def _queue_positions() -> dict[str, int]:
    """Map of pending job_id → 0-indexed position, ordered by created_at.

    Recomputed per request — cheap (linear over the job dirs) and avoids
    keeping a separate in-memory queue in sync with state.json.
    """
    pending = sorted(
        (s for s in storage.list_states(_jobs_root()) if s["status"] == "pending"),
        key=lambda s: s["created_at"],
    )
    return {s["id"]: i for i, s in enumerate(pending)}


def _progress_from_state(state: dict) -> Progress:
    log_path = storage.job_dir(_jobs_root(), state["id"]) / "log.txt"
    completed = storage.parse_completed_runs(
        log_path, state["status"], state.get("total_runs", 1),
    )
    return Progress(
        completed_runs=completed,
        total_runs=state.get("total_runs", 1),
    )


def _state_to_summary(state: dict, queue_position: int | None) -> JobSummary:
    return JobSummary(
        id=state["id"],
        name=state["name"],
        status=JobStatus(state["status"]),
        created_at=state["created_at"],
        progress=_progress_from_state(state),
        queue_position=queue_position,
    )


def _state_to_job(state: dict, queue_position: int | None) -> Job:
    return Job(
        id=state["id"],
        name=state["name"],
        status=JobStatus(state["status"]),
        created_at=state["created_at"],
        started_at=state.get("started_at"),
        completed_at=state.get("completed_at"),
        progress=_progress_from_state(state),
        jobs_config=[JobConfig(**jc) for jc in state.get("jobs_config", [])],
        targets_count=state.get("targets_count", 0),
        queue_position=queue_position,
        error=state.get("error"),
    )


def _err(status_code: int, error: str, message: str, **details) -> JSONResponse:
    body: dict = {"error": error, "message": message}
    if details:
        body["details"] = details
    return JSONResponse(status_code=status_code, content=body)


def _not_found(job_id: str) -> JSONResponse:
    return _err(404, "not_found", f"no job with id {job_id}")


# ────────────────────────────────────────────────
# Routes
# ────────────────────────────────────────────────


@app.get("/api/jobs", response_model=JobListResponse)
async def list_jobs(
    status: JobStatus | None = None,
    limit: int = Query(50, ge=1, le=200),
):
    states = storage.list_states(_jobs_root())
    if status is not None:
        states = [s for s in states if s["status"] == status.value]
    states.sort(key=lambda s: s["created_at"], reverse=True)
    states = states[:limit]
    qp = _queue_positions()
    summaries = [_state_to_summary(s, qp.get(s["id"])) for s in states]

    all_states = storage.list_states(_jobs_root())
    return JobListResponse(
        jobs=summaries,
        concurrency=ConcurrencyInfo(
            running=sum(1 for s in all_states if s["status"] == "running"),
            queued=len(qp),
            max=_manager().max_concurrent,
        ),
    )


@app.post("/api/jobs", response_model=Job, status_code=201)
async def create_job(
    manifest: UploadFile = File(...),
    posts: UploadFile = File(...),
    name: str | None = Form(None),
):
    manifest_bytes = await manifest.read()
    posts_bytes = await posts.read()

    job_id = storage.new_job_id()
    try:
        m, targets_count = storage.save_manifest_and_posts(
            _jobs_root(), job_id, manifest_bytes, posts_bytes,
        )
    except (ValidationError, ValueError, yaml.YAMLError) as e:
        storage.delete_job_dir(_jobs_root(), job_id)
        return _err(400, "validation_error", str(e))

    # Refuse early if the manifest expects proxy creds the server doesn't
    # have — avoids a guaranteed-fail subprocess minutes into a run.
    if m.proxy is not None:
        from ranker.proxy import env_var_names
        u, p = env_var_names(m.proxy.provider)
        if not (os.environ.get(u) and os.environ.get(p)):
            storage.delete_job_dir(_jobs_root(), job_id)
            return _err(
                400, "env_missing",
                f"manifest carries 'proxy:' but server is missing {u} and/or {p}",
            )

    resolved = resolve_jobs(m)
    state = {
        "id": job_id,
        "name": name or f"job_{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}",
        "status": "pending",
        "created_at": _now_iso(),
        "total_runs": m.schedule.count,
        "jobs_config": [{"name": rj.name, "mode": rj.mode.value} for rj in resolved],
        "targets_count": targets_count,
    }
    storage.write_state(_jobs_root(), job_id, state)
    await _manager().submit(job_id)

    qp = _queue_positions().get(job_id)
    return _state_to_job(state, qp)


@app.get("/api/jobs/{job_id}", response_model=Job)
async def get_job(job_id: str):
    state = storage.read_state(_jobs_root(), job_id)
    if state is None:
        return _not_found(job_id)
    return _state_to_job(state, _queue_positions().get(job_id))


@app.delete("/api/jobs/{job_id}", status_code=204)
async def delete_job(job_id: str):
    state = storage.read_state(_jobs_root(), job_id)
    if state is None:
        return _not_found(job_id)
    if state["status"] in ("running", "pending"):
        await _manager().cancel(job_id)
    else:
        storage.delete_job_dir(_jobs_root(), job_id)
    return None


@app.get("/api/jobs/{job_id}/result")
async def get_job_result(job_id: str):
    state = storage.read_state(_jobs_root(), job_id)
    if state is None:
        return _not_found(job_id)
    output_path = storage.job_dir(_jobs_root(), job_id) / "output.yaml"
    if not output_path.exists():
        return _err(
            409, "no_result_yet",
            f"job {job_id} has not produced output yet (status={state['status']})",
        )
    return FileResponse(
        output_path,
        media_type="application/yaml",
        filename=f"ranks_{job_id}.yaml",
    )


@app.get("/api/jobs/{job_id}/logs", response_class=PlainTextResponse)
async def get_job_logs(
    job_id: str,
    tail: int | None = Query(None, ge=1, le=10000),
):
    state = storage.read_state(_jobs_root(), job_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"no job with id {job_id}")
    log_path = storage.job_dir(_jobs_root(), job_id) / "log.txt"
    if not log_path.exists():
        return ""
    text = log_path.read_text(encoding="utf-8", errors="replace")
    if tail:
        text = "\n".join(text.splitlines()[-tail:])
    return text
