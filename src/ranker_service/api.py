"""FastAPI HTTP layer — routes implement docs/openapi.yaml.

State lives on disk (state.json per job) so the server can be restarted
without losing track of finished work. Running subprocesses, however,
do NOT survive restarts in this MVP — anything that was ``running``
when the server died will appear stuck. A future revision can scan at
startup and mark orphans as ``failed``.
"""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import yaml
from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import ValidationError

from ranker.manifest import resolve_jobs

from . import storage
from .jobs import JobManager, _now_iso
from .models import (
    CacheStats,
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


def _reap_orphans(jobs_root: Path) -> int:
    """Mark stuck-running and stuck-pending jobs as failed.

    Subprocesses don't survive a server restart, so any state.json that
    still says ``running`` belongs to a dead subprocess; ``pending``
    jobs were queued inside the previous JobManager (which is gone)
    and will never start on their own. Both look indistinguishable
    from a healthy active job in the UI — flipping them to ``failed``
    with a clear error message makes the dashboard correct again and
    tells the user to re-submit.

    Returns the number of jobs reaped (zero on a clean shutdown, N
    after a crash). The count is logged at startup so the operator
    knows whether the previous server died mid-flight.
    """
    reaped = 0
    for state in storage.list_states(jobs_root):
        if state["status"] in ("running", "pending"):
            storage.update_state(
                jobs_root, state["id"],
                status="failed",
                completed_at=_now_iso(),
                error="orphaned by server restart — re-submit to retry",
            )
            reaped += 1
    return reaped


# Lifespan-managed singletons. App state is held on the FastAPI app
# rather than as module globals so test suites can spin up multiple
# isolated apps if needed.
@asynccontextmanager
async def lifespan(app: FastAPI):
    jobs_root, max_concurrent = _config()
    app.state.jobs_root = jobs_root
    reaped = _reap_orphans(jobs_root)
    if reaped > 0:
        # stderr so it shows up in uvicorn's log alongside its own
        # startup banner; flush so it appears even if buffered.
        print(
            f"[ranker-service] reaped {reaped} orphaned job(s) "
            f"from previous server lifetime",
            file=sys.stderr,
            flush=True,
        )
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


def _cache_stats_for(job_id: str) -> CacheStats | None:
    """Read the runner-written cache_stats.yaml if it exists yet.

    Returns None when the file is absent (caching wasn't enabled, or
    the run hasn't reached its finally block) or unreadable. We don't
    surface a parse error to the client — better to show "no stats yet"
    than an error on a perfectly valid Job.
    """
    path = storage.job_dir(_jobs_root(), job_id) / "cache_stats.yaml"
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        return CacheStats(
            total_hits=data["total_hits"],
            total_misses=data["total_misses"],
            total_stored=data["total_stored"],
            total_bytes_saved=data["total_bytes_saved"],
            hit_rate=data["hit_rate"],
        )
    except (OSError, yaml.YAMLError, KeyError, TypeError):
        return None


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
        cache_stats=_cache_stats_for(state["id"]),
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


@app.get("/api/jobs/{job_id}/cache-stats")
async def get_job_cache_stats(job_id: str):
    """Download the cache savings file (YAML) the runner wrote.

    Mirrors ``/result`` so a user who clicks "결과" and "캐시 통계"
    gets two files of the same shape. ``cache_stats.yaml`` is written
    by the runner's ``finally`` block, so it's available the moment
    a Job leaves "running" — even on cancel/fail.
    """
    state = storage.read_state(_jobs_root(), job_id)
    if state is None:
        return _not_found(job_id)
    stats_path = storage.job_dir(_jobs_root(), job_id) / "cache_stats.yaml"
    if not stats_path.exists():
        return _err(
            409, "no_cache_stats",
            f"job {job_id} has no cache stats "
            f"(caching off or run hasn't completed yet, status={state['status']})",
        )
    return FileResponse(
        stats_path,
        media_type="application/yaml",
        filename=f"cache_stats_{job_id}.yaml",
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


# Terminal states a Job may finish in. Used by the log streamer to
# decide when to drain the file one last time and close the WebSocket.
_TERMINAL_STATES: frozenset[str] = frozenset({"completed", "failed", "cancelled"})

# How often the streamer re-stats the log file looking for new bytes.
# 300ms is well under human latency tolerance ("real-time enough") and
# costs almost nothing — one stat() per active connection per tick.
_LOG_POLL_S = 0.3

# Initial backlog sent on connect — matches the HTTP endpoint's default
# tail size so the WS view picks up where the polling view used to.
_LOG_INITIAL_TAIL = 200


@app.websocket("/api/jobs/{job_id}/logs/stream")
async def stream_job_logs(websocket: WebSocket, job_id: str):
    """Stream a job's log as it is written.

    On connect: send the last ``_LOG_INITIAL_TAIL`` lines (matches the
    HTTP endpoint's default ``?tail=200``). Then poll the log file every
    ``_LOG_POLL_S`` seconds and forward any newly-appended bytes. When
    the Job reaches a terminal state, drain any final bytes and close
    cleanly with code 1000.

    Failure modes:
    - Job not found → close with 4404 before accept (the WS handshake
      still completes; the close frame carries the reason).
    - Client disconnects mid-stream → caught and returned silently.
    - Unexpected errors → close with 1011 (server error). The server
      keeps running; only this connection dies.
    """
    await websocket.accept()
    try:
        state = storage.read_state(_jobs_root(), job_id)
        if state is None:
            await websocket.close(code=4404, reason="job not found")
            return

        log_path = storage.job_dir(_jobs_root(), job_id) / "log.txt"

        # Wait for the subprocess to actually create the log file. A
        # freshly-submitted job has a state.json but no log yet — the
        # CLI subprocess writes its first line a moment later.
        while not log_path.exists():
            state = storage.read_state(_jobs_root(), job_id)
            if state is None or state["status"] in _TERMINAL_STATES:
                # Job finished without producing logs (rare — usually
                # means the subprocess died before writing anything).
                await websocket.close()
                return
            await asyncio.sleep(_LOG_POLL_S)

        # Initial tail. Reading the whole file once is fine — it's
        # bounded by however long the job has been running, and we
        # only do this on connect.
        text = log_path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines(keepends=True)
        initial = (
            "".join(lines[-_LOG_INITIAL_TAIL:])
            if len(lines) > _LOG_INITIAL_TAIL
            else text
        )
        if initial:
            await websocket.send_text(initial)
        pos = log_path.stat().st_size

        # Tail-follow loop — poll the file size, forward any new bytes,
        # exit when the Job is done. Reading bytes (not text) and
        # decoding here means we never split a multi-byte UTF-8
        # sequence across two sends.
        while True:
            try:
                current_size = log_path.stat().st_size
            except FileNotFoundError:
                # Log file was removed (job dir cleanup race) — nothing
                # more to send.
                break

            if current_size < pos:
                # File truncated/replaced. Re-anchor; we don't try to
                # resend earlier history because the tail we already
                # sent matches what was there.
                pos = 0
            if current_size > pos:
                with open(log_path, "rb") as f:
                    f.seek(pos)
                    new_bytes = f.read()
                pos = log_path.stat().st_size
                await websocket.send_text(
                    new_bytes.decode("utf-8", errors="replace"),
                )

            state = storage.read_state(_jobs_root(), job_id)
            if state is None or state["status"] in _TERMINAL_STATES:
                # Final drain — bytes may have been written between
                # the size check above and the state check just now.
                try:
                    final_size = log_path.stat().st_size
                except FileNotFoundError:
                    break
                if final_size > pos:
                    with open(log_path, "rb") as f:
                        f.seek(pos)
                        await websocket.send_text(
                            f.read().decode("utf-8", errors="replace"),
                        )
                break

            await asyncio.sleep(_LOG_POLL_S)

        await websocket.close()
    except WebSocketDisconnect:
        # Client closed the tab / refreshed — perfectly normal.
        return
    except Exception:
        # Unexpected. Best-effort close so the client sees a clean
        # error frame instead of a hung connection.
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
