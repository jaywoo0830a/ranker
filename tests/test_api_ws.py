"""WebSocket log-streaming API contract.

Covers ``/api/jobs/{job_id}/logs/stream`` end-to-end against the FastAPI
TestClient. The streamer's contract:
  - on connect with an unknown job → close 4404
  - on connect with an existing job whose log file exists → first frame
    carries the initial tail, then any new bytes appended to the file
    arrive as further frames
  - when the Job state transitions to terminal, the streamer drains and
    closes cleanly (1000)
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


def _write_state(job_dir: Path, status: str) -> None:
    (job_dir / "state.json").write_text(
        json.dumps({
            "id": job_dir.name,
            "name": job_dir.name,
            "status": status,
            "created_at": "2026-04-27T21:00:00+00:00",
            "total_runs": 1,
            "jobs_config": [],
            "targets_count": 0,
        }),
        encoding="utf-8",
    )


def _make_job(jobs_root: Path, job_id: str, status: str, log: str = "") -> Path:
    d = jobs_root / job_id
    d.mkdir(parents=True, exist_ok=True)
    _write_state(d, status)
    if log:
        (d / "log.txt").write_text(log, encoding="utf-8")
    return d


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    """Fresh app instance pointed at an isolated jobs dir per test.

    We re-import api inside the fixture so the env-var read in
    ``_config()`` picks up the tmp path. ``sys.modules`` is reset on
    teardown so other tests are unaffected.
    """
    monkeypatch.setenv("RANKER_SERVICE_JOBS_DIR", str(tmp_path))
    monkeypatch.setenv("RANKER_SERVICE_MAX_CONCURRENT_JOBS", "1")
    import importlib
    import sys
    sys.modules.pop("ranker_service.api", None)
    api = importlib.import_module("ranker_service.api")
    with TestClient(api.app) as c:
        yield c, tmp_path
    sys.modules.pop("ranker_service.api", None)


class TestLogStream:
    def test_unknown_job_closes_immediately(self, client):
        c, _ = client
        with pytest.raises(WebSocketDisconnect) as exc:
            with c.websocket_connect("/api/jobs/does-not-exist/logs/stream") as ws:
                # Server accepts, then sends close frame with 4404. The
                # first receive raises WebSocketDisconnect carrying the
                # code we set.
                ws.receive_text()
        assert exc.value.code == 4404

    def test_terminal_job_sends_tail_then_closes(self, client):
        c, root = client
        log = "first line\nsecond line\nthird line\n"
        _make_job(root, "done1", status="completed", log=log)

        with c.websocket_connect("/api/jobs/done1/logs/stream") as ws:
            received = ws.receive_text()
            assert received == log
            # After the initial tail and the terminal-state check, the
            # server drains and closes — next receive raises.
            with pytest.raises(WebSocketDisconnect):
                ws.receive_text()

    def test_running_job_streams_appended_bytes(self, client):
        c, root = client
        d = _make_job(root, "live1", status="running", log="initial\n")

        with c.websocket_connect("/api/jobs/live1/logs/stream") as ws:
            assert ws.receive_text() == "initial\n"

            # Simulate the subprocess writing more lines.
            with open(d / "log.txt", "a", encoding="utf-8") as f:
                f.write("more line\n")

            # Streamer polls at 300ms; give it a few cycles.
            received = ""
            deadline = time.time() + 3.0
            while time.time() < deadline and "more line" not in received:
                received += ws.receive_text()
            assert "more line" in received

            # Now flip to terminal — streamer should drain + close.
            _write_state(d, status="completed")
            with pytest.raises(WebSocketDisconnect):
                # May receive one more chunk before close; loop until close.
                deadline = time.time() + 3.0
                while time.time() < deadline:
                    ws.receive_text()

    def test_log_file_appears_after_connect(self, client):
        # State exists ("running") but the subprocess hasn't created
        # log.txt yet — the streamer must wait, not 404.
        c, root = client
        d = _make_job(root, "wait1", status="running")  # no log content

        with c.websocket_connect("/api/jobs/wait1/logs/stream") as ws:
            # Create the file shortly after connect.
            (d / "log.txt").write_text("late start\n", encoding="utf-8")
            received = ""
            deadline = time.time() + 3.0
            while time.time() < deadline and "late start" not in received:
                received += ws.receive_text()
            assert "late start" in received
