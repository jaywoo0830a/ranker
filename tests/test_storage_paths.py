"""Storage-layer path rewriting on manifest save.

The web layer rewrites three manifest paths so concurrent Jobs don't
collide and so the disk cache is shared across Jobs:

  - ``targets.path``  → absolute path to the saved posts file
  - ``output.path``   → absolute path inside the Job's private dir
  - ``resources.cache.dir`` → absolute path to a server-managed shared
    cache (only when the manifest opted in to caching)

These tests pin those rewrites so a future refactor can't silently
revert one of them and re-introduce per-Job caches or path collisions.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from ranker_service import storage


def _base_manifest() -> dict:
    return {
        "version": 1,
        "schedule": {"count": 1, "interval": "1h", "start": "immediate"},
        "source": {"kind": "naver_unified_search"},
        "targets": {"source": "file", "path": "should-be-overridden.yaml"},
        "output": {"path": "should-be-overridden.yaml"},
    }


def _save(jobs_root: Path, raw: dict, posts: list | None = None) -> dict:
    posts_yaml = yaml.safe_dump(posts or []).encode("utf-8")
    storage.save_manifest_and_posts(
        jobs_root, "job_x",
        yaml.safe_dump(raw).encode("utf-8"),
        posts_yaml,
    )
    saved = yaml.safe_load(
        (jobs_root / "job_x" / "manifest.yaml").read_text(encoding="utf-8"),
    )
    return saved


class TestSharedCacheDir:
    def test_default_is_hidden_dir_inside_jobs_root(self, tmp_path: Path):
        assert storage.shared_cache_dir(tmp_path) == (tmp_path / ".shared-cache").resolve()

    def test_env_var_overrides_default(self, tmp_path: Path, monkeypatch):
        # Ops can repoint to /var/cache/ranker etc. without touching code.
        monkeypatch.setenv("RANKER_SERVICE_CACHE_DIR", str(tmp_path / "ops_cache"))
        assert storage.shared_cache_dir(tmp_path) == (tmp_path / "ops_cache").resolve()


class TestSaveManifestPathOverrides:
    def test_targets_path_rewritten_to_saved_posts(self, tmp_path: Path):
        saved = _save(tmp_path, _base_manifest())
        expected = (tmp_path / "job_x" / "posts.yaml").absolute()
        assert saved["targets"]["path"] == str(expected)
        assert saved["targets"]["source"] == "file"

    def test_output_path_rewritten_to_job_dir(self, tmp_path: Path):
        saved = _save(tmp_path, _base_manifest())
        expected = (tmp_path / "job_x" / "output.yaml").absolute()
        assert saved["output"]["path"] == str(expected)


class TestCacheDirOverride:
    def test_cache_dir_rewritten_to_shared_root(self, tmp_path: Path):
        # Same shared dir for every Job → disk hit on the second Job
        # to request a given URL, regardless of which Job stored it.
        raw = _base_manifest()
        raw["resources"] = {
            "cache": {
                "enabled": True,
                "dir": ".ranker-cache",  # user wrote a relative path
                "domains": ["pstatic.net"],
            },
        }
        saved = _save(tmp_path, raw)
        assert saved["resources"]["cache"]["dir"] == str(
            storage.shared_cache_dir(tmp_path),
        )

    def test_env_var_propagates_to_saved_manifest(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("RANKER_SERVICE_CACHE_DIR", str(tmp_path / "ops"))
        raw = _base_manifest()
        raw["resources"] = {"cache": {"enabled": True, "domains": ["pstatic.net"]}}
        saved = _save(tmp_path, raw)
        assert saved["resources"]["cache"]["dir"] == str((tmp_path / "ops").resolve())

    def test_no_resources_block_unchanged(self, tmp_path: Path):
        # Manifest without ``resources`` shouldn't grow one — that would
        # silently change opt-in behavior (now blocking nothing, but
        # carrying a structurally surprising diff).
        saved = _save(tmp_path, _base_manifest())
        assert "resources" not in saved

    def test_resources_without_cache_block_untouched(self, tmp_path: Path):
        # User chose blocking but not caching — don't add a cache stub.
        # Schema allows resources.cache to be absent and that's a
        # meaningful "caching off" signal we must preserve.
        raw = _base_manifest()
        raw["resources"] = {"block": ["image"], "block_third_party_trackers": True}
        saved = _save(tmp_path, raw)
        assert "cache" not in saved["resources"]

    def test_user_supplied_absolute_dir_is_still_overridden(self, tmp_path: Path):
        # Even an absolute path the user wrote gets replaced — the
        # webapp owns the cache location to guarantee cross-Job sharing.
        # Documented elsewhere; this test pins the behavior.
        raw = _base_manifest()
        raw["resources"] = {
            "cache": {"enabled": True, "dir": "/tmp/user-chosen", "domains": ["pstatic.net"]},
        }
        saved = _save(tmp_path, raw)
        assert saved["resources"]["cache"]["dir"] == str(
            storage.shared_cache_dir(tmp_path),
        )
        assert saved["resources"]["cache"]["dir"] != "/tmp/user-chosen"


# ────────────────────────────────────────────────
# cache_stats.json surfacing
# ────────────────────────────────────────────────

class TestCacheStatsAPI:
    def test_job_response_omits_cache_stats_when_file_absent(self, tmp_path, monkeypatch):
        # Job exists but cache wasn't enabled (no cache_stats.json) —
        # the API must not fabricate a stats block.
        monkeypatch.setenv("RANKER_SERVICE_JOBS_DIR", str(tmp_path))
        import importlib
        import sys
        sys.modules.pop("ranker_service.api", None)
        api = importlib.import_module("ranker_service.api")
        from fastapi.testclient import TestClient
        import json as _json

        # Hand-write a minimal job state to avoid a real subprocess.
        d = tmp_path / "job_x"
        d.mkdir()
        (d / "state.json").write_text(_json.dumps({
            "id": "job_x", "name": "job_x", "status": "completed",
            "created_at": "2026-04-27T21:00:00+00:00",
            "total_runs": 1, "jobs_config": [], "targets_count": 0,
        }), encoding="utf-8")

        with TestClient(api.app) as c:
            res = c.get("/api/jobs/job_x")
            assert res.status_code == 200
            assert res.json()["cache_stats"] is None
        sys.modules.pop("ranker_service.api", None)

    def test_job_response_includes_cache_stats_when_file_present(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RANKER_SERVICE_JOBS_DIR", str(tmp_path))
        import importlib
        import sys
        sys.modules.pop("ranker_service.api", None)
        api = importlib.import_module("ranker_service.api")
        from fastapi.testclient import TestClient
        import json as _json

        d = tmp_path / "job_y"
        d.mkdir()
        (d / "state.json").write_text(_json.dumps({
            "id": "job_y", "name": "job_y", "status": "completed",
            "created_at": "2026-04-27T21:00:00+00:00",
            "total_runs": 1, "jobs_config": [], "targets_count": 0,
        }), encoding="utf-8")
        (d / "cache_stats.yaml").write_text(yaml.safe_dump({
            "total_hits": 124,
            "total_misses": 18,
            "total_stored": 18,
            "total_bytes_saved": 12 * 1024 * 1024,
            "hit_rate": 0.873,
            "by_run": [],
        }), encoding="utf-8")

        with TestClient(api.app) as c:
            res = c.get("/api/jobs/job_y")
            assert res.status_code == 200
            cs = res.json()["cache_stats"]
            assert cs["total_hits"] == 124
            assert cs["total_bytes_saved"] == 12 * 1024 * 1024
            assert 0.87 < cs["hit_rate"] < 0.88
        sys.modules.pop("ranker_service.api", None)

    def test_corrupt_cache_stats_returns_none_not_500(self, tmp_path, monkeypatch):
        # A garbage cache_stats.yaml must not break the Job endpoint —
        # surfacing an internal error here would mask the actual Job
        # state from the user.
        monkeypatch.setenv("RANKER_SERVICE_JOBS_DIR", str(tmp_path))
        import importlib
        import sys
        sys.modules.pop("ranker_service.api", None)
        api = importlib.import_module("ranker_service.api")
        from fastapi.testclient import TestClient
        import json as _json

        d = tmp_path / "job_z"
        d.mkdir()
        (d / "state.json").write_text(_json.dumps({
            "id": "job_z", "name": "job_z", "status": "completed",
            "created_at": "2026-04-27T21:00:00+00:00",
            "total_runs": 1, "jobs_config": [], "targets_count": 0,
        }), encoding="utf-8")
        # Tab in indented context is the simplest YAML loader trip-up.
        (d / "cache_stats.yaml").write_text("\tnot: valid", encoding="utf-8")

        with TestClient(api.app) as c:
            res = c.get("/api/jobs/job_z")
            assert res.status_code == 200
            assert res.json()["cache_stats"] is None
        sys.modules.pop("ranker_service.api", None)


class TestCacheStatsDownload:
    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RANKER_SERVICE_JOBS_DIR", str(tmp_path))
        import importlib
        import sys
        sys.modules.pop("ranker_service.api", None)
        api = importlib.import_module("ranker_service.api")
        return api

    def test_download_serves_yaml_with_filename(self, tmp_path, monkeypatch):
        # The mirror of /result — a real <a download> link in the webapp.
        # File must come back as application/yaml, with a sensible default
        # filename so the browser saves something a human can identify.
        api = self._setup(tmp_path, monkeypatch)
        from fastapi.testclient import TestClient
        import json as _json

        d = tmp_path / "job_dl"
        d.mkdir()
        (d / "state.json").write_text(_json.dumps({
            "id": "job_dl", "name": "job_dl", "status": "completed",
            "created_at": "2026-04-27T21:00:00+00:00",
            "total_runs": 1, "jobs_config": [], "targets_count": 0,
        }), encoding="utf-8")
        payload = {
            "total_hits": 5, "total_misses": 1, "total_stored": 1,
            "total_bytes_saved": 1024, "hit_rate": 0.833, "by_run": [],
        }
        (d / "cache_stats.yaml").write_text(
            yaml.safe_dump(payload), encoding="utf-8",
        )

        with TestClient(api.app) as c:
            res = c.get("/api/jobs/job_dl/cache-stats")
            assert res.status_code == 200
            assert res.headers["content-type"].startswith("application/yaml")
            assert "cache_stats_job_dl.yaml" in res.headers.get(
                "content-disposition", "",
            )
            # Body round-trips through YAML.
            body = yaml.safe_load(res.content.decode("utf-8"))
            assert body["total_hits"] == 5
        import sys
        sys.modules.pop("ranker_service.api", None)

    def test_download_404_for_unknown_job(self, tmp_path, monkeypatch):
        api = self._setup(tmp_path, monkeypatch)
        from fastapi.testclient import TestClient
        with TestClient(api.app) as c:
            res = c.get("/api/jobs/nope/cache-stats")
            assert res.status_code == 404
        import sys
        sys.modules.pop("ranker_service.api", None)

    def test_download_409_when_stats_not_yet_written(self, tmp_path, monkeypatch):
        # Job exists, caching off (or run hasn't reached finally) →
        # 409 with a message the UI can show, not a 500.
        api = self._setup(tmp_path, monkeypatch)
        from fastapi.testclient import TestClient
        import json as _json

        d = tmp_path / "job_pending"
        d.mkdir()
        (d / "state.json").write_text(_json.dumps({
            "id": "job_pending", "name": "job_pending", "status": "running",
            "created_at": "2026-04-27T21:00:00+00:00",
            "total_runs": 1, "jobs_config": [], "targets_count": 0,
        }), encoding="utf-8")

        with TestClient(api.app) as c:
            res = c.get("/api/jobs/job_pending/cache-stats")
            # Reaper flips the running state to failed at lifespan
            # startup, so the endpoint sees a terminal job with no
            # cache stats — still 409, just a different status in
            # the message text.
            assert res.status_code == 409
            assert res.json()["error"] == "no_cache_stats"
        import sys
        sys.modules.pop("ranker_service.api", None)


# ────────────────────────────────────────────────
# Orphan reaper — flip dead-subprocess jobs to failed at startup
# ────────────────────────────────────────────────

class TestOrphanReaper:
    """Subprocesses don't survive a uvicorn restart. Anything left in
    ``running``/``pending`` after the previous lifespan ended is dead;
    the reaper flips it to ``failed`` with a clear error so the UI
    stops showing a stuck progress bar."""

    def _make(self, tmp_path: Path, job_id: str, status: str) -> Path:
        import json as _json
        d = tmp_path / job_id
        d.mkdir()
        (d / "state.json").write_text(_json.dumps({
            "id": job_id, "name": job_id, "status": status,
            "created_at": "2026-04-27T21:00:00+00:00",
            "total_runs": 1, "jobs_config": [], "targets_count": 0,
        }), encoding="utf-8")
        return d

    def _read_status(self, job_dir: Path) -> str:
        import json as _json
        return _json.loads((job_dir / "state.json").read_text())["status"]

    def _read_state(self, job_dir: Path) -> dict:
        import json as _json
        return _json.loads((job_dir / "state.json").read_text())

    def test_running_jobs_get_reaped_to_failed(self, tmp_path: Path):
        from ranker_service.api import _reap_orphans
        d = self._make(tmp_path, "job_run", "running")
        reaped = _reap_orphans(tmp_path)
        assert reaped == 1
        state = self._read_state(d)
        assert state["status"] == "failed"
        assert "orphaned" in state["error"]
        assert "re-submit" in state["error"]
        # completed_at stamped so the UI can show "ended at X".
        assert state["completed_at"]

    def test_pending_jobs_get_reaped_too(self, tmp_path: Path):
        # Pending = queued in the previous JobManager which is now
        # gone. Without reaping, these sit in the queue forever
        # since the new manager has empty internal state.
        from ranker_service.api import _reap_orphans
        d = self._make(tmp_path, "job_pend", "pending")
        _reap_orphans(tmp_path)
        assert self._read_status(d) == "failed"

    def test_terminal_states_left_alone(self, tmp_path: Path):
        # completed/failed/cancelled are already final — touching them
        # would rewrite history and confuse the audit trail.
        from ranker_service.api import _reap_orphans
        for status in ("completed", "failed", "cancelled"):
            d = self._make(tmp_path, f"job_{status}", status)
            _reap_orphans(tmp_path)
            assert self._read_status(d) == status

    def test_mixed_states_reaped_correctly(self, tmp_path: Path):
        from ranker_service.api import _reap_orphans
        d_run = self._make(tmp_path, "job_a", "running")
        d_pend = self._make(tmp_path, "job_b", "pending")
        d_done = self._make(tmp_path, "job_c", "completed")
        d_fail = self._make(tmp_path, "job_d", "failed")
        reaped = _reap_orphans(tmp_path)
        assert reaped == 2  # only running + pending
        assert self._read_status(d_run) == "failed"
        assert self._read_status(d_pend) == "failed"
        assert self._read_status(d_done) == "completed"
        assert self._read_status(d_fail) == "failed"

    def test_empty_jobs_dir_returns_zero(self, tmp_path: Path):
        from ranker_service.api import _reap_orphans
        assert _reap_orphans(tmp_path) == 0

    def test_lifespan_runs_reaper_on_startup(self, tmp_path: Path, monkeypatch):
        # End-to-end: a "running" state.json in a fresh server's jobs
        # dir is reaped before the first request lands.
        monkeypatch.setenv("RANKER_SERVICE_JOBS_DIR", str(tmp_path))
        self._make(tmp_path, "job_orphan", "running")
        import importlib
        import sys
        sys.modules.pop("ranker_service.api", None)
        api = importlib.import_module("ranker_service.api")
        from fastapi.testclient import TestClient
        with TestClient(api.app) as c:
            res = c.get("/api/jobs/job_orphan")
            assert res.status_code == 200
            body = res.json()
            assert body["status"] == "failed"
            assert "orphaned" in body["error"]
        sys.modules.pop("ranker_service.api", None)
