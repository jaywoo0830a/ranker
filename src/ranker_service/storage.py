"""File-based persistence — one directory per job.

Layout::

    {jobs_root}/
      {job_id}/
        manifest.yaml   ← uploaded YAML, with output.path/targets.path
                          rewritten so the subprocess writes inside this
                          directory (uploads carrying ``examples/ranks.yaml``
                          would otherwise collide across jobs).
        posts.yaml      ← uploaded report file
        output.yaml     ← runner's incremental result (created by subprocess)
        log.txt         ← captured stderr from the subprocess
        state.json      ← canonical Job state (status, timestamps, etc.)

``state.json`` is the source of truth for status — both the API layer and
the JobManager read/write it through the helpers here.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
from pathlib import Path

import yaml

from ranker.manifest import Manifest


def new_job_id() -> str:
    """Server-assigned id; matches the ``^job_[a-zA-Z0-9]{6,}$`` pattern
    in docs/openapi.yaml."""
    return f"job_{secrets.token_hex(4)}"


def job_dir(jobs_root: Path, job_id: str) -> Path:
    return jobs_root / job_id


def shared_cache_dir(jobs_root: Path) -> Path:
    """Server-managed cross-Job script cache root.

    All Jobs spawned by this server share one disk cache, so a JS bundle
    fetched by one Job's first run is served from disk for every other
    Job that visits the same URL. Without this, ``cache.dir`` defaults
    to a relative path that resolves under each Job's private cwd and
    the cache stays per-Job (defeating the point of caching).

    Configurable via ``RANKER_SERVICE_CACHE_DIR`` for ops who want
    /var/cache/ranker or similar; defaults to a hidden directory inside
    the jobs root so it lives on the same volume as job state.
    """
    override = os.environ.get("RANKER_SERVICE_CACHE_DIR")
    if override:
        return Path(override).resolve()
    return (jobs_root / ".shared-cache").resolve()


def write_state(jobs_root: Path, job_id: str, state: dict) -> None:
    path = job_dir(jobs_root, job_id) / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, default=str), encoding="utf-8")


def read_state(jobs_root: Path, job_id: str) -> dict | None:
    path = job_dir(jobs_root, job_id) / "state.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def update_state(jobs_root: Path, job_id: str, **fields) -> dict:
    state = read_state(jobs_root, job_id) or {}
    state.update(fields)
    write_state(jobs_root, job_id, state)
    return state


def list_states(jobs_root: Path) -> list[dict]:
    if not jobs_root.exists():
        return []
    out: list[dict] = []
    for d in jobs_root.iterdir():
        if d.is_dir():
            s = read_state(jobs_root, d.name)
            if s is not None:
                out.append(s)
    return out


def save_manifest_and_posts(
    jobs_root: Path,
    job_id: str,
    manifest_bytes: bytes,
    posts_bytes: bytes,
) -> tuple[Manifest, int]:
    """Persist uploads. Rewrites manifest paths so the subprocess writes
    inside this job's private directory regardless of what the user put
    in the YAML.

    Returns the parsed Manifest plus the number of targets, which the API
    surfaces in the create response.
    """
    d = job_dir(jobs_root, job_id)
    d.mkdir(parents=True, exist_ok=True)

    posts_path = d / "posts.yaml"
    posts_path.write_bytes(posts_bytes)

    raw = yaml.safe_load(manifest_bytes)
    if not isinstance(raw, dict):
        raise ValueError("manifest must be a YAML mapping at the top level")

    # Force targets to point at the saved posts file (absolute path so
    # subprocess cwd doesn't matter).
    raw.setdefault("targets", {})
    raw["targets"]["source"] = "file"
    raw["targets"]["path"] = str(posts_path.absolute())

    # Force output to the per-job result file. Mode preserved if user set
    # it; we don't second-guess append vs overwrite.
    raw.setdefault("output", {})
    raw["output"]["path"] = str((d / "output.yaml").absolute())

    # Steer cache.dir at a server-managed shared location so a JS bundle
    # fetched by one Job is served from disk for every other Job that
    # visits the same URL. Without this override, ``.ranker-cache``
    # resolves relative to the subprocess cwd (= per-Job dir) and the
    # cache is private — one fetch per Job per URL, defeating the point.
    # We only inject this when the user opted in to caching; manifests
    # without a ``resources.cache`` block stay untouched.
    resources = raw.get("resources")
    if isinstance(resources, dict) and isinstance(resources.get("cache"), dict):
        resources["cache"]["dir"] = str(shared_cache_dir(jobs_root))

    manifest_path = d / "manifest.yaml"
    manifest_path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    # Validate via the project's Pydantic schema — same error surface as
    # the CLI would raise.
    manifest = Manifest.model_validate(raw)
    targets_raw = yaml.safe_load(posts_bytes) or []
    targets_count = len(targets_raw) if isinstance(targets_raw, list) else 0
    return manifest, targets_count


def delete_job_dir(jobs_root: Path, job_id: str) -> bool:
    d = job_dir(jobs_root, job_id)
    if not d.exists():
        return False
    shutil.rmtree(d)
    return True


_RUN_LINE = re.compile(r"^\[ranker\] run (\d+)/(\d+)$", re.MULTILINE)


def parse_completed_runs(log_path: Path, status: str, total_runs: int) -> int:
    """Best-effort progress estimate from runner stderr.

    The runner prints ``[ranker] run N/M`` at the start of each scheduled
    run. We treat ``N-1`` as completed while in flight, and ``total`` once
    the subprocess exits cleanly.
    """
    if status == "completed":
        return total_runs
    if not log_path.exists():
        return 0
    text = log_path.read_text(encoding="utf-8", errors="replace")
    runs_seen = 0
    for m in _RUN_LINE.finditer(text):
        runs_seen = max(runs_seen, int(m.group(1)))
    return max(0, runs_seen - 1)
