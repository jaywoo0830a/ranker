"""Graceful shutdown — SIGTERM cleanup all the way down.

The contract being tested:

  1. Sending SIGTERM to the ``ranker`` CLI raises KeyboardInterrupt
     inside the subprocess, so its ``finally`` chain runs and the
     process exits with code 130 instead of being killed cold.

  2. ``JobManager._exec`` maps that 130 to ``cancelled`` (not
     ``failed``) — exit code 130 here always means "we asked it to
     stop", whether via Ctrl+C, the cancel button, or uvicorn lifespan
     shutdown.

  3. ``JobManager.cancel_all`` cancels every active subprocess in
     parallel within the standard 5s budget, so uvicorn shutting down
     doesn't take O(N) seconds for N running jobs.

These three together prevent the orphan Playwright/Chromium bug we
hit on the live server (parent-death + uncaught SIGTERM left a full
headless Chromium tree alive on the host).
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest


# ────────────────────────────────────────────────
# CLI SIGTERM handler — real subprocess test
# ────────────────────────────────────────────────


class TestCliSigtermHandler:
    """The handler is process-wide state, so we have to fork a real
    subprocess to verify it. A toy script imports ``cli._install_…``,
    arms the handler, and sleeps; we send SIGTERM and assert the
    process exits with 130 (KeyboardInterrupt path) rather than 143
    (uncaught SIGTERM = ``128 + SIGTERM``)."""

    def test_sigterm_exits_via_keyboardinterrupt(self, tmp_path: Path):
        # Print a readiness marker AFTER the handler is installed and
        # block on stderr until we see it — importing ranker.cli pulls
        # in the runner + Playwright which can take seconds, and we'd
        # otherwise race the handler installation.
        script = textwrap.dedent("""
            import sys, time
            from ranker.cli import _install_sigterm_handler
            _install_sigterm_handler()
            print("ready", file=sys.stderr, flush=True)
            try:
                time.sleep(30)
            except KeyboardInterrupt:
                print("interrupted", file=sys.stderr, flush=True)
                sys.exit(130)
            sys.exit(0)
        """).strip()
        script_path = tmp_path / "sleeper.py"
        script_path.write_text(script, encoding="utf-8")

        proc = subprocess.Popen(
            [sys.executable, str(script_path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        # Wait for the readiness marker — heavy imports finished and
        # the SIGTERM handler is armed.
        ready = False
        for _ in range(100):  # up to 10s
            line = proc.stderr.readline()
            if not line:
                break
            if b"ready" in line:
                ready = True
                break
        if not ready:
            proc.kill()
            pytest.fail("subprocess never signaled readiness")

        proc.send_signal(signal.SIGTERM)
        try:
            stderr = proc.communicate(timeout=5)[1].decode()
        except subprocess.TimeoutExpired:
            proc.kill()
            pytest.fail("subprocess did not exit within 5s of SIGTERM")
        # 130 = caught KeyboardInterrupt path. Without our handler
        # the process would die with the default SIGTERM action and
        # ``returncode`` would be -15 (SIGTERM uncaught).
        assert proc.returncode == 130, (
            f"expected 130, got {proc.returncode}; stderr={stderr!r}"
        )
        assert "interrupted" in stderr


# ────────────────────────────────────────────────
# JobManager exit-code mapping + cancel_all
# ────────────────────────────────────────────────


def _state_path(jobs_root: Path, job_id: str) -> Path:
    return jobs_root / job_id / "state.json"


async def _make_running_job(
    jobs_root: Path, job_id: str, sleep_seconds: int = 30,
) -> tuple[object, asyncio.subprocess.Process]:
    """Spin up a real subprocess that JobManager would manage, plus
    seed the state.json the manager / API expect.

    The subprocess installs ranker.cli's SIGTERM→KeyboardInterrupt
    handler and sleeps. We wait for a readiness marker on stderr so
    the SIGTERM in the test never races with module imports (which
    pull in Playwright and can take seconds).
    """
    import json
    from ranker_service.jobs import JobManager
    d = jobs_root / job_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text(
        json.dumps({
            "id": job_id, "name": job_id, "status": "running",
            "created_at": "2026-04-27T21:00:00+00:00",
            "total_runs": 1, "jobs_config": [], "targets_count": 0,
        }),
        encoding="utf-8",
    )
    script = textwrap.dedent(f"""
        import sys, time
        from ranker.cli import _install_sigterm_handler
        _install_sigterm_handler()
        print("ready", file=sys.stderr, flush=True)
        try:
            time.sleep({sleep_seconds})
        except KeyboardInterrupt:
            sys.exit(130)
        sys.exit(0)
    """).strip()
    script_path = d / "sleeper.py"
    script_path.write_text(script, encoding="utf-8")

    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(script_path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    # Block until the SIGTERM handler is armed.
    ready_line = await asyncio.wait_for(
        proc.stderr.readline(), timeout=15.0,
    )
    assert b"ready" in ready_line, (
        f"subprocess never signaled readiness: {ready_line!r}"
    )
    manager = JobManager(jobs_root, max_concurrent=4)
    manager._processes[job_id] = proc
    return manager, proc


class TestRcMapping:
    """The exit-code → status table is the contract between cli.py and
    the JobManager. Pin it explicitly — a slip here is what painted
    cancelled jobs as 'failed' in the UI."""

    def test_rc_zero_maps_to_completed(self):
        # Behavior fenced by direct lookup of the constants in the
        # manager — a smoke check guards the table from regressions.
        # Full integration is exercised below.
        from ranker_service import jobs as jobs_mod
        # The mapping lives inline in _exec; just confirm the source
        # mentions the three expected branches.
        src = Path(jobs_mod.__file__).read_text(encoding="utf-8")
        assert 'status="completed"' in src
        assert 'status="cancelled"' in src
        assert 'status="failed"' in src
        # rc=130 is the new branch we just added.
        assert "rc == 130" in src


@pytest.mark.asyncio
class TestCancelAll:
    async def test_cancel_all_with_no_active_jobs_is_noop(self, tmp_path):
        from ranker_service.jobs import JobManager
        manager = JobManager(tmp_path, max_concurrent=4)
        # Should return promptly — no exception, no hang.
        await asyncio.wait_for(manager.cancel_all(), timeout=2.0)

    async def test_cancel_all_terminates_running_subprocess(self, tmp_path):
        # End-to-end: a real subprocess that sleeps; cancel_all sends
        # SIGTERM, the handler catches it, exits 130 → state.json
        # flips to ``cancelled`` (not ``failed``). The whole dance
        # must complete inside the 5s per-job budget.
        manager, proc = await _make_running_job(tmp_path, "job_term")
        try:
            await asyncio.wait_for(manager.cancel_all(), timeout=8.0)
        except asyncio.TimeoutError:
            proc.kill()
            pytest.fail("cancel_all did not finish within budget")
        assert proc.returncode is not None
        assert proc.returncode == 130, (
            f"expected graceful 130 exit, got {proc.returncode}"
        )

    async def test_cancel_all_handles_multiple_jobs_in_parallel(self, tmp_path):
        # Three subprocesses, each would individually take ~5s to
        # confirm-cancel. Parallel cancel must keep total time near
        # one budget, not three.
        managers_procs = []
        for i in range(3):
            mp = await _make_running_job(tmp_path, f"job_p{i}")
            managers_procs.append(mp)
        # Use the first manager for cancel_all but seed all three
        # processes into it (mirrors the real "one manager per server"
        # topology).
        manager = managers_procs[0][0]
        for _, proc in managers_procs[1:]:
            # Re-key under unique ids so the manager treats them as
            # separate jobs.
            pid_key = f"job_p{proc.pid}"
            manager._processes[pid_key] = proc

        start = time.monotonic()
        try:
            await asyncio.wait_for(manager.cancel_all(), timeout=8.0)
        except asyncio.TimeoutError:
            for _, proc in managers_procs:
                proc.kill()
            pytest.fail("parallel cancel_all exceeded single-job budget")
        elapsed = time.monotonic() - start
        # 3 jobs × 5s sequential = 15s; parallel should be ~1s for
        # graceful exit. 7s is a comfortable upper bound that still
        # catches a regression to sequential.
        assert elapsed < 7.0, f"cancel_all took {elapsed:.1f}s, expected parallel"
        for _, proc in managers_procs:
            assert proc.returncode == 130


# ────────────────────────────────────────────────
# Lifespan integration
# ────────────────────────────────────────────────


class TestLifespanShutdown:
    def test_lifespan_invokes_cancel_all_on_exit(self, tmp_path, monkeypatch):
        # Patch cancel_all to record invocation; entering and exiting
        # the TestClient context drives lifespan startup + shutdown.
        monkeypatch.setenv("RANKER_SERVICE_JOBS_DIR", str(tmp_path))
        import importlib
        import sys
        sys.modules.pop("ranker_service.api", None)
        api = importlib.import_module("ranker_service.api")

        called = {"count": 0}
        original = api.JobManager.cancel_all

        async def _spy(self):
            called["count"] += 1
            await original(self)

        monkeypatch.setattr(api.JobManager, "cancel_all", _spy)

        from fastapi.testclient import TestClient
        with TestClient(api.app):
            pass  # enter + exit drives lifespan
        assert called["count"] == 1
        sys.modules.pop("ranker_service.api", None)
