"""Job lifecycle: queue → semaphore → subprocess → finalize.

One asyncio.Semaphore caps how many subprocesses run at once. Each
``submit()`` schedules a task that awaits the semaphore; FIFO acquisition
order (Python 3.10+) means submission order is also queue order, which
the API surfaces as ``queue_position``.

Live subprocesses are tracked in ``_processes`` so ``cancel()`` can
SIGTERM them. Pending jobs (still waiting on the semaphore) are tracked
in ``_tasks`` so the same call can drop them from the queue cleanly.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import storage


def _ranker_command(manifest_path: Path) -> tuple[str, ...]:
    """Resolve how to invoke the ranker CLI.

    Prefers ``RANKER_BIN`` (e.g. ``/usr/local/bin/ranker`` in Docker).
    Falls back to ``python -m ranker.cli`` so the API works even when the
    script isn't on PATH (common in local dev when you run uvicorn from
    a different shell than the venv was activated in).
    """
    bin_override = os.environ.get("RANKER_BIN")
    if bin_override:
        return (bin_override, str(manifest_path))
    return (sys.executable, "-m", "ranker.cli", str(manifest_path))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobManager:
    def __init__(self, jobs_root: Path, max_concurrent: int):
        self.jobs_root = jobs_root
        self.max_concurrent = max_concurrent
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    async def submit(self, job_id: str) -> None:
        """Schedule the job for execution when a slot frees up."""
        task = asyncio.create_task(self._run(job_id), name=f"job-{job_id}")
        self._tasks[job_id] = task

    async def _run(self, job_id: str) -> None:
        try:
            async with self._semaphore:
                await self._exec(job_id)
        except asyncio.CancelledError:
            # Cancelled while still pending — nothing to clean up; cancel()
            # already wrote the cancelled state.
            return
        finally:
            self._tasks.pop(job_id, None)

    async def _exec(self, job_id: str) -> None:
        d = storage.job_dir(self.jobs_root, job_id)
        log_path = d / "log.txt"
        manifest_path = d / "manifest.yaml"

        storage.update_state(
            self.jobs_root, job_id,
            status="running", started_at=_now_iso(),
        )

        cmd = _ranker_command(manifest_path)
        log_fp = open(log_path, "w", encoding="utf-8", buffering=1)
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=log_fp,
                cwd=str(d),
            )
            self._processes[job_id] = process
            rc = await process.wait()
        finally:
            log_fp.close()
            self._processes.pop(job_id, None)

        if rc == 0:
            storage.update_state(
                self.jobs_root, job_id,
                status="completed", completed_at=_now_iso(), error=None,
            )
        elif rc < 0 or rc == 130:
            # ``rc < 0`` = killed by uncaught signal (SIGKILL = -9).
            # ``rc == 130`` = SIGINT/SIGTERM caught by the CLI, which
            # ran graceful cleanup (closing Playwright) and exited via
            # the conventional ``128 + SIGINT`` code. Both cases are
            # "we asked it to stop" — surface as cancelled, not failed,
            # so the UI doesn't paint a red error on an intentional stop.
            storage.update_state(
                self.jobs_root, job_id,
                status="cancelled", completed_at=_now_iso(), error=None,
            )
        else:
            tail = self._log_tail(log_path)
            storage.update_state(
                self.jobs_root, job_id,
                status="failed", completed_at=_now_iso(),
                error=tail or f"subprocess exited {rc}",
            )

    @staticmethod
    def _log_tail(log_path: Path) -> str:
        if not log_path.exists():
            return ""
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            return lines[-1] if lines else ""
        except OSError:
            return ""

    async def cancel(self, job_id: str) -> None:
        """SIGTERM if running, otherwise drop from the queue."""
        process = self._processes.get(job_id)
        if process is not None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                process.kill()
            return

        task = self._tasks.pop(job_id, None)
        if task is not None and not task.done():
            task.cancel()
        storage.update_state(
            self.jobs_root, job_id,
            status="cancelled", completed_at=_now_iso(),
        )

    async def cancel_all(self) -> None:
        """Cancel every running and pending job in parallel.

        Used at server shutdown so each subprocess catches its SIGTERM,
        runs the runner's ``finally`` chain (closing Playwright +
        Chromium), and exits cleanly within 5s — instead of getting
        SIGKILL'd by the OS when uvicorn's parent dies and leaving
        orphaned headless Chromium behind.

        Per-cancel timeout is the same 5s as the single-job ``cancel``;
        running them via ``gather`` keeps total shutdown time bounded
        regardless of how many jobs were active.
        """
        ids = list(self._processes.keys()) + list(self._tasks.keys())
        if not ids:
            return
        await asyncio.gather(
            *(self.cancel(jid) for jid in ids),
            return_exceptions=True,
        )

    def is_running(self, job_id: str) -> bool:
        return job_id in self._processes
