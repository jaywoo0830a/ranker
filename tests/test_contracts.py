"""Contract tests — the manifest schema and the pure matching logic.

Network / Playwright behavior is covered separately; here we prove the
declarative surface and the rank-matching policy are correct in isolation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from ranker.event import RankQuery
from ranker.event import Engagement, RankResult, VisitResult
from ranker.manifest import (
    Cache,
    JobOverride,
    MatchBy,
    Manifest,
    Mode,
    OutputMode,
    Proxy,
    ProxyProvider,
    Resources,
    ResolvedJob,
    ResourceType,
    RotatePolicy,
    TargetItem,
    Targets,
    TargetSourceKind,
    load_manifest,
    parse_duration,
    resolve_jobs,
)
from ranker.profile import DESKTOP, MOBILE, profile_for
from ranker.proxy import (
    build_proxy_arg,
    build_username,
    env_var_names,
    is_transient_proxy_error,
    new_session_id,
    require_credentials,
)
from ranker.resources import (
    BlockCounter,
    CacheCounter,
    DiskCache,
    _is_cacheable_request,
    _is_cacheable_response,
    is_tracker_host,
    make_route_handler,
)
from ranker.runner import _due_status, _result_to_run_dict, persist_results
from ranker.search import extract_blog_id, match_rank, normalize_items


# ────────────────────────────────────────────────
# Duration parsing
# ────────────────────────────────────────────────

class TestParseDuration:
    def test_minutes(self):
        assert parse_duration("60min").total_seconds() == 3600

    def test_seconds(self):
        assert parse_duration("30s").total_seconds() == 30

    def test_hours(self):
        assert parse_duration("2h").total_seconds() == 7200

    def test_compound(self):
        assert parse_duration("1h30m").total_seconds() == 5400

    def test_unknown_unit_rejected(self):
        with pytest.raises(ValueError):
            parse_duration("5foo")


# ────────────────────────────────────────────────
# Manifest DSL
# ────────────────────────────────────────────────

def _example_manifest_dict() -> dict:
    return {
        "version": 1,
        "schedule": {"count": 3, "interval": "60min"},
        "source": {
            "kind": "naver_unified_search",
            "sections": {
                "head": {"type": "css", "value": "div.spw_rerank._slog_visible._rra_head"},
                "body": {"type": "css", "value": "div.spw_rerank._slog_visible._rra_body"},
            },
        },
        "targets": {"source": "file", "path": "posts.yaml"},
        "output": {"path": "ranks.yaml"},
    }


def _minimal_mobile_dict() -> dict:
    """Shows the 'mode: mobile + omit selectors' flow — profile fills in."""
    return {
        "version": 1,
        "mode": "mobile",
        "schedule": {"count": 1, "interval": "60min"},
        "source": {"kind": "naver_unified_search"},
        "targets": {"source": "file", "path": "posts.yaml"},
        "output": {"path": "ranks.yaml"},
    }


class TestManifest:
    def test_minimal_manifest_has_defaults(self):
        m = Manifest.model_validate(_example_manifest_dict())
        assert m.matching.by == MatchBy.BLOG_ID
        assert m.output.mode == OutputMode.APPEND
        assert m.identity.rotate_context == RotatePolicy.PER_RUN
        # 10s floor — satisfies the Firewall behavior layer
        assert m.behavior.dwell_ms.min == 10000
        assert m.post_visit.enabled is False
        assert m.post_visit.dwell_ms.min == 160000

    def test_post_visit_can_be_enabled_with_custom_ranges(self):
        data = _example_manifest_dict()
        data["post_visit"] = {
            "enabled": True,
            "dwell_ms": {"min": 1000, "max": 2000},
            "mouse_events": {"min": 2, "max": 5},
        }
        m = Manifest.model_validate(data)
        assert m.post_visit.enabled is True
        assert m.post_visit.dwell_ms.max == 2000
        # scroll defaults to reading-style when not overridden.
        assert m.post_visit.scroll.depth_ratio.min == 0.7
        assert m.post_visit.scroll.depth_ratio.max == 0.95
        assert m.post_visit.scroll.down_bias == 0.85

    def test_scroll_policy_custom_depth(self):
        data = _example_manifest_dict()
        data["post_visit"] = {
            "enabled": True,
            "scroll": {
                "depth_ratio": {"min": 0.5, "max": 0.6},
                "down_bias": 0.6,
            },
        }
        m = Manifest.model_validate(data)
        assert m.post_visit.scroll.depth_ratio.min == 0.5
        assert m.post_visit.scroll.down_bias == 0.6

    def test_scroll_depth_rejects_out_of_range(self):
        data = _example_manifest_dict()
        data["post_visit"] = {
            "enabled": True,
            "scroll": {"depth_ratio": {"min": 0.5, "max": 1.5}},
        }
        with pytest.raises(Exception):
            Manifest.model_validate(data)

    def test_scroll_depth_rejects_inverted_range(self):
        data = _example_manifest_dict()
        data["post_visit"] = {
            "enabled": True,
            "scroll": {"depth_ratio": {"min": 0.9, "max": 0.5}},
        }
        with pytest.raises(Exception):
            Manifest.model_validate(data)


# ────────────────────────────────────────────────
# Mode + profile
# ────────────────────────────────────────────────

    def test_unknown_field_is_rejected(self):
        data = _example_manifest_dict()
        data["schedule"]["typo_field"] = 42
        with pytest.raises(Exception):
            Manifest.model_validate(data)

    def test_inline_targets_require_items(self):
        data = _example_manifest_dict()
        data["targets"] = {"source": "inline"}
        with pytest.raises(Exception):
            Manifest.model_validate(data)

    def test_file_targets_require_path(self):
        data = _example_manifest_dict()
        data["targets"] = {"source": "file"}
        with pytest.raises(Exception):
            Manifest.model_validate(data)

    def test_behavior_range_must_be_ordered(self):
        data = _example_manifest_dict()
        data["behavior"] = {"dwell_ms": {"min": 9000, "max": 5000}}
        with pytest.raises(Exception):
            Manifest.model_validate(data)

    def test_interval_rejects_garbage(self):
        data = _example_manifest_dict()
        data["schedule"]["interval"] = "not a duration"
        with pytest.raises(Exception):
            Manifest.model_validate(data)

    def test_example_manifest_loads(self, tmp_path: Path):
        path = tmp_path / "job.yaml"
        path.write_text(yaml.safe_dump(_example_manifest_dict()))
        m = load_manifest(path)
        assert m.schedule.count == 3
        assert m.schedule.interval_delta.total_seconds() == 3600
        assert "_rra_head" in m.source.sections.head.value
        assert "_rra_body" in m.source.sections.body.value

    def test_sections_both_required(self):
        data = _example_manifest_dict()
        data["source"]["sections"] = {
            "head": {"type": "css", "value": ".head"},
        }
        with pytest.raises(Exception):
            Manifest.model_validate(data)


# ────────────────────────────────────────────────
# Mode + profile
# ────────────────────────────────────────────────

class TestMode:
    def test_default_mode_is_desktop(self):
        m = Manifest.model_validate(_example_manifest_dict())
        assert m.mode == Mode.DESKTOP

    def test_mode_mobile_parses(self):
        data = _example_manifest_dict()
        data["mode"] = "mobile"
        m = Manifest.model_validate(data)
        assert m.mode == Mode.MOBILE

    def test_sections_can_be_omitted_for_profile_default(self):
        m = Manifest.model_validate(_minimal_mobile_dict())
        # sections omitted — profile will supply them at runtime.
        assert m.source.sections is None

    def test_viewport_can_be_omitted_for_profile_default(self):
        m = Manifest.model_validate(_minimal_mobile_dict())
        assert m.identity.viewport is None


class TestProfile:
    def test_desktop_profile_matches_enum(self):
        assert profile_for(Mode.DESKTOP) is DESKTOP

    def test_mobile_profile_matches_enum(self):
        assert profile_for(Mode.MOBILE) is MOBILE

    def test_desktop_is_not_mobile(self):
        assert DESKTOP.is_mobile is False
        assert DESKTOP.has_touch is False
        assert DESKTOP.viewport.width == 1280

    def test_mobile_is_mobile_and_touch(self):
        assert MOBILE.is_mobile is True
        assert MOBILE.has_touch is True
        assert MOBILE.viewport.width == 414
        assert "m.search.naver.com" in MOBILE.search_url_template

    def test_desktop_search_url_uses_desktop_host(self):
        assert "search.naver.com" in DESKTOP.search_url_template
        assert "m.search.naver.com" not in DESKTOP.search_url_template

    def test_search_url_template_has_query_placeholder(self):
        assert "{query}" in DESKTOP.search_url_template
        assert "{query}" in MOBILE.search_url_template


# ────────────────────────────────────────────────
# blog_id extraction
# ────────────────────────────────────────────────

class TestExtractBlogId:
    def test_canonical_desktop_url(self):
        assert extract_blog_id("https://blog.naver.com/myblog/223456789") == "myblog"

    def test_mobile_url(self):
        assert extract_blog_id("https://m.blog.naver.com/myblog/223456789") == "myblog"

    def test_query_style_url(self):
        url = "https://blog.naver.com/PostView.naver?blogId=myblog&logNo=223456789"
        assert extract_blog_id(url) == "myblog"

    def test_unrelated_url(self):
        assert extract_blog_id("https://cafe.naver.com/foo/123") is None


# ────────────────────────────────────────────────
# match_rank: order-preserving, 1-indexed, reason-carrying
# ────────────────────────────────────────────────

def _q(blog_id: str = "myblog", title: str = "안녕") -> RankQuery:
    return RankQuery(blog_id=blog_id, keyword="test", title=title)


class TestMatchRank:
    def test_match_by_blog_id_first_occurrence_wins(self):
        items = [
            {"url": "https://blog.naver.com/other/1", "title": "x"},
            {"url": "https://blog.naver.com/myblog/2", "title": "y"},
            {"url": "https://blog.naver.com/myblog/3", "title": "z"},
        ]
        rank, reason = match_rank(items, _q(), MatchBy.BLOG_ID)
        assert rank == 2
        assert "blog_id" in reason

    def test_no_match_carries_scanned_depth(self):
        items = [
            {"url": "https://blog.naver.com/other/1", "title": "x"},
            {"url": "https://blog.naver.com/another/2", "title": "y"},
        ]
        rank, reason = match_rank(items, _q(), MatchBy.BLOG_ID)
        assert rank is None
        assert "top 2" in reason

    def test_match_by_title_exact(self):
        items = [
            {"url": "https://blog.naver.com/anyone/1", "title": "강남역 맛집 BEST 10"},
        ]
        rank, reason = match_rank(items, _q(title="강남역 맛집 BEST 10"), MatchBy.TITLE)
        assert rank == 1
        assert "title" in reason

    def test_combined_match_requires_both(self):
        items = [
            {"url": "https://blog.naver.com/myblog/1", "title": "다른 제목"},
            {"url": "https://blog.naver.com/myblog/2", "title": "맞는 제목"},
        ]
        rank, _ = match_rank(
            items,
            _q(title="맞는 제목"),
            MatchBy.BLOG_ID_AND_TITLE,
        )
        assert rank == 2

    def test_empty_items_returns_none(self):
        rank, reason = match_rank([], _q(), MatchBy.BLOG_ID)
        assert rank is None
        assert "top 0" in reason


# ────────────────────────────────────────────────
# normalize_items: dedup + junk filter
# ────────────────────────────────────────────────

class TestNormalizeItems:
    def test_drops_non_blog_links(self):
        raw = [
            {"url": "https://keep.naver.com/", "title": "Keep에 바로가기"},
            {"url": "https://cafe.naver.com/sppo", "title": "카페"},
            {"url": "https://blog.naver.com/lssettle/224258599658", "title": "글"},
        ]
        slots = normalize_items(raw, scan_depth=10)
        assert len(slots) == 1
        assert slots[0]["blog_id"] == "lssettle"

    def test_dedupes_profile_and_post_into_one_slot(self):
        raw = [
            {"url": "https://blog.naver.com/lssettle", "title": "프로필"},
            {"url": "https://blog.naver.com/lssettle/224258599658", "title": "글 제목"},
        ]
        slots = normalize_items(raw, scan_depth=10)
        assert len(slots) == 1
        # Post anchor wins so title-based matching keeps working.
        assert slots[0]["title"] == "글 제목"
        assert slots[0]["is_post"] is True

    def test_scan_depth_limits_unique_slots(self):
        raw = [
            {"url": "https://blog.naver.com/a/1", "title": "1"},
            {"url": "https://blog.naver.com/b/2", "title": "2"},
            {"url": "https://blog.naver.com/c/3", "title": "3"},
        ]
        slots = normalize_items(raw, scan_depth=2)
        assert [s["blog_id"] for s in slots] == ["a", "b"]

    def test_post_anchor_is_chosen_even_when_appearing_after_profile(self):
        raw = [
            {"url": "https://blog.naver.com/a", "title": ""},
            {"url": "https://blog.naver.com/a/123", "title": "제목"},
        ]
        slots = normalize_items(raw, scan_depth=10)
        assert slots[0]["url"].endswith("/123")
        assert slots[0]["title"] == "제목"

    def test_real_naver_head_block_shape(self):
        """Regression: matches the structure observed from live search."""
        raw = [
            {"url": "https://blog.naver.com/lssettle", "title": "프로필1"},
            {"url": "https://search.naver.com/...#", "title": "Keep에 저장"},
            {"url": "https://keep.naver.com/", "title": "Keep에 바로가기"},
            {"url": "https://blog.naver.com/lssettle/224258599658", "title": "글1"},
            {"url": "https://blog.naver.com/throatr5b", "title": "프로필2"},
            {"url": "https://blog.naver.com/throatr5b/224247969668", "title": "글2"},
            {"url": "https://blog.naver.com/erestriction", "title": "프로필3"},
            {"url": "https://blog.naver.com/erestriction/224244573017", "title": "글3"},
            {"url": "https://blog.naver.com/eventap", "title": "프로필4"},
            {"url": "https://blog.naver.com/eventap/224247045257", "title": "글4"},
        ]
        slots = normalize_items(raw, scan_depth=10)
        assert [s["blog_id"] for s in slots] == [
            "lssettle", "throatr5b", "erestriction", "eventap",
        ]
        # Each slot should carry the post (not profile) URL.
        assert all(s["is_post"] for s in slots)


# ────────────────────────────────────────────────
# Result → YAML run dict shaping
# ────────────────────────────────────────────────

def _hit_result(**overrides) -> RankResult:
    base = dict(
        blog_id="x", keyword="k", title="t", checked_at="2026-04-24T15:00:00+09:00",
        section="body", rank=5, url="https://blog.naver.com/x/1",
        reason="matched by blog_id in body", visit=None,
    )
    base.update(overrides)
    return RankResult(**base)


class TestResultToRunDict:
    def test_miss_omits_url_and_visit(self):
        r = RankResult(
            blog_id="x", keyword="k", title="t",
            checked_at="2026-04-24T15:00:00+09:00",
            section=None, rank=None, url=None, reason="not found", visit=None,
        )
        run = _result_to_run_dict(r)
        assert "url" not in run
        assert "visit" not in run
        assert run["rank"] is None

    def test_hit_without_visit_includes_url_only(self):
        run = _result_to_run_dict(_hit_result())
        assert run["url"].endswith("/x/1")
        assert "visit" not in run

    def test_hit_with_visit_includes_visit_block(self):
        r = _hit_result(
            visit=VisitResult(
                visited_at="2026-04-24T15:01:30+09:00",
                url="https://blog.naver.com/x/1",
                dwelled_ms=178_430,
            ),
        )
        run = _result_to_run_dict(r)
        assert run["visit"]["dwelled_ms"] == 178_430
        assert "engagement" not in run["visit"]

    def test_visit_with_engagement_is_included(self):
        r = _hit_result(
            visit=VisitResult(
                visited_at="2026-04-24T15:01:30+09:00",
                url="https://blog.naver.com/x/1",
                dwelled_ms=180_000,
                engagement=Engagement(views=142, likes=3, comments=1),
            ),
        )
        run = _result_to_run_dict(r)
        assert run["visit"]["engagement"] == {"views": 142, "likes": 3, "comments": 1}


# ────────────────────────────────────────────────
# Proxy schema + username assembly
# ────────────────────────────────────────────────

def _proxy_dict() -> dict:
    return {
        "provider": "proxyempire",
        "host": "v2.proxyempire.io",
        "port": 5000,
        "country": "kr",
        "session_ttl": "30m",
    }


class TestProxySchema:
    def test_minimal_proxy_uses_defaults(self):
        p = Proxy.model_validate({"host": "v2.proxyempire.io", "port": 5000})
        assert p.provider == ProxyProvider.PROXYEMPIRE
        assert p.country == "kr"
        assert p.session_ttl == "30m"
        assert p.session_ttl_minutes == 30

    def test_session_ttl_below_minimum_rejected(self):
        data = _proxy_dict()
        data["session_ttl"] = "30s"
        with pytest.raises(Exception):
            Proxy.model_validate(data)

    def test_session_ttl_above_maximum_rejected(self):
        data = _proxy_dict()
        data["session_ttl"] = "61m"
        with pytest.raises(Exception):
            Proxy.model_validate(data)

    def test_session_ttl_at_boundaries_accepted(self):
        data = _proxy_dict()
        data["session_ttl"] = "1m"
        assert Proxy.model_validate(data).session_ttl_minutes == 1
        data["session_ttl"] = "60m"
        assert Proxy.model_validate(data).session_ttl_minutes == 60

    def test_country_must_be_lowercase_iso(self):
        for bad in ["KR", "k", "kor", "k1"]:
            data = _proxy_dict()
            data["country"] = bad
            with pytest.raises(Exception):
                Proxy.model_validate(data)

    def test_unknown_provider_rejected(self):
        data = _proxy_dict()
        data["provider"] = "brightdata"
        with pytest.raises(Exception):
            Proxy.model_validate(data)

    def test_port_out_of_range_rejected(self):
        data = _proxy_dict()
        data["port"] = 70000
        with pytest.raises(Exception):
            Proxy.model_validate(data)

    def test_unknown_field_rejected(self):
        data = _proxy_dict()
        data["api_key"] = "leaked-secret"
        with pytest.raises(Exception):
            Proxy.model_validate(data)


class TestProxyOnManifest:
    def test_manifest_without_proxy_block_is_dev_mode(self):
        m = Manifest.model_validate(_example_manifest_dict())
        assert m.proxy is None

    def test_manifest_with_proxy_block(self):
        data = _example_manifest_dict()
        data["proxy"] = _proxy_dict()
        m = Manifest.model_validate(data)
        assert m.proxy is not None
        assert m.proxy.host == "v2.proxyempire.io"
        assert m.proxy.session_ttl_minutes == 30

    def test_proxy_block_does_not_carry_credentials(self):
        # Belt-and-suspenders: schema rejects username/password on the
        # manifest so secrets cannot accidentally land on disk.
        for leaky_key in ("username", "password", "api_key", "credentials"):
            data = _example_manifest_dict()
            data["proxy"] = {**_proxy_dict(), leaky_key: "oops"}
            with pytest.raises(Exception):
                Manifest.model_validate(data)


class TestUsernameAssembly:
    def _proxy(self, **overrides) -> Proxy:
        data = _proxy_dict()
        data.update(overrides)
        return Proxy.model_validate(data)

    def test_username_format_matches_proxyempire_spec(self):
        u = build_username(self._proxy(), "m_e08f2b2931", "abcd1234")
        assert u == "m_e08f2b2931-country-kr-sid-abcd1234-ttl-30m"

    def test_ttl_minutes_floor_for_subminute_inputs_blocked_at_schema_level(self):
        # Schema already rejects <1m, but verify the assembler also reflects
        # whatever the schema accepted.
        p = self._proxy(session_ttl="15m")
        u = build_username(p, "m_acct", "deadbeef")
        assert "-ttl-15m" in u

    def test_country_propagates(self):
        p = self._proxy(country="us")
        u = build_username(p, "m_acct", "deadbeef")
        assert "-country-us-" in u

    def test_session_ids_are_unique_across_calls(self):
        ids = {new_session_id() for _ in range(50)}
        assert len(ids) == 50

    def test_session_id_is_hex_8(self):
        sid = new_session_id()
        assert len(sid) == 8
        int(sid, 16)  # valid hex, raises if not


class TestProxyArg:
    def test_build_proxy_arg_shape(self):
        p = Proxy.model_validate(_proxy_dict())
        arg = build_proxy_arg(p, "m_acct", "secret-pw", "abcd1234")
        pw = arg.to_playwright()
        assert pw["server"] == "http://v2.proxyempire.io:5000"
        assert pw["username"] == "m_acct-country-kr-sid-abcd1234-ttl-30m"
        assert pw["password"] == "secret-pw"

    def test_repr_does_not_leak_password(self):
        p = Proxy.model_validate(_proxy_dict())
        arg = build_proxy_arg(p, "m_acct", "super-secret-password", "abcd1234")
        r = repr(arg)
        assert "super-secret-password" not in r
        assert "***" in r
        # Account stem is also semi-sensitive — only the SID value shows.
        assert "m_acct" not in r
        assert "abcd1234" in r


class TestRequireCredentials:
    def test_env_var_names_for_proxyempire(self):
        assert env_var_names(ProxyProvider.PROXYEMPIRE) == (
            "PROXYEMPIRE_USERNAME",
            "PROXYEMPIRE_PASSWORD",
        )

    def test_returns_credentials_when_both_set(self, monkeypatch):
        monkeypatch.setenv("PROXYEMPIRE_USERNAME", "m_acct")
        monkeypatch.setenv("PROXYEMPIRE_PASSWORD", "pw123")
        assert require_credentials(ProxyProvider.PROXYEMPIRE) == ("m_acct", "pw123")

    def test_raises_when_username_missing(self, monkeypatch):
        monkeypatch.delenv("PROXYEMPIRE_USERNAME", raising=False)
        monkeypatch.setenv("PROXYEMPIRE_PASSWORD", "pw123")
        with pytest.raises(RuntimeError, match="PROXYEMPIRE_USERNAME"):
            require_credentials(ProxyProvider.PROXYEMPIRE)

    def test_raises_when_password_missing(self, monkeypatch):
        monkeypatch.setenv("PROXYEMPIRE_USERNAME", "m_acct")
        monkeypatch.delenv("PROXYEMPIRE_PASSWORD", raising=False)
        with pytest.raises(RuntimeError, match="PROXYEMPIRE_PASSWORD"):
            require_credentials(ProxyProvider.PROXYEMPIRE)

    def test_error_lists_both_missing_vars(self, monkeypatch):
        monkeypatch.delenv("PROXYEMPIRE_USERNAME", raising=False)
        monkeypatch.delenv("PROXYEMPIRE_PASSWORD", raising=False)
        with pytest.raises(RuntimeError) as exc:
            require_credentials(ProxyProvider.PROXYEMPIRE)
        assert "PROXYEMPIRE_USERNAME" in str(exc.value)
        assert "PROXYEMPIRE_PASSWORD" in str(exc.value)


class TestExampleManifests:
    """Smoke-load the shipped examples — catches schema breakage."""

    def test_dev_yaml_loads_without_proxy(self):
        m = load_manifest(Path("examples/dev.yaml"))
        assert m.proxy is None

    def test_prod_yaml_loads_with_proxy_and_jobs(self):
        m = load_manifest(Path("examples/prod.yaml"))
        # prod is the canonical multi-Job + proxy + post_visit recipe; this
        # test catches schema breakage in any of those three pieces.
        assert m.proxy is not None
        assert m.proxy.provider == ProxyProvider.PROXYEMPIRE
        assert m.proxy.country == "kr"
        assert m.proxy.session_ttl_minutes == 30
        assert m.jobs is not None
        assert [j.name for j in m.jobs] == [
            "desktop-1", "desktop-2", "mobile-1", "mobile-2",
        ]
        assert m.post_visit.enabled is True
        # cache enabled on the static-CDN whitelist — guards against
        # accidental deletion of the cache block from the canonical manifest.
        assert m.resources is not None
        assert m.resources.cache is not None
        assert m.resources.cache.enabled is True
        assert "pstatic.net" in m.resources.cache.domains


# ────────────────────────────────────────────────
# Transient proxy error classification
# ────────────────────────────────────────────────

class TestIsTransientProxyError:
    def test_tunnel_failure_is_transient(self):
        e = Exception(
            "Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED at https://example.com/"
        )
        assert is_transient_proxy_error(e) is True

    def test_proxy_connection_failed_is_transient(self):
        e = Exception("net::ERR_PROXY_CONNECTION_FAILED at https://x")
        assert is_transient_proxy_error(e) is True

    def test_timeout_is_transient(self):
        e = Exception("net::ERR_TIMED_OUT at https://x")
        assert is_transient_proxy_error(e) is True

    def test_connection_reset_is_transient(self):
        e = Exception("net::ERR_CONNECTION_RESET")
        assert is_transient_proxy_error(e) is True

    def test_proxy_auth_required_is_NOT_transient(self):
        # Auth failure won't get better on retry — it's a config problem.
        e = Exception("net::ERR_PROXY_AUTH_REQUESTED")
        assert is_transient_proxy_error(e) is False

    def test_proxy_cert_invalid_is_NOT_transient(self):
        e = Exception("net::ERR_PROXY_CERTIFICATE_INVALID")
        assert is_transient_proxy_error(e) is False

    def test_unrelated_error_is_NOT_transient(self):
        # Don't let arbitrary failures get retried as if they were proxy
        # flap — the retry would just hide a real bug.
        e = ValueError("blog_id missing from URL")
        assert is_transient_proxy_error(e) is False
        e2 = Exception("net::ERR_NAME_NOT_RESOLVED")
        assert is_transient_proxy_error(e2) is False

    def test_message_carrying_both_markers_treats_permanent_as_authoritative(self):
        # Defensive: an error that mentions both auth and tunnel should
        # NOT retry — the auth signal wins.
        e = Exception("net::ERR_PROXY_AUTH_REQUESTED, ERR_TUNNEL_CONNECTION_FAILED")
        assert is_transient_proxy_error(e) is False


# ────────────────────────────────────────────────
# Job DSL — schema, resolution, backward compatibility
# ────────────────────────────────────────────────

class TestJobOverride:
    def test_minimal_job_just_needs_a_name(self):
        j = JobOverride.model_validate({"name": "x"})
        assert j.name == "x"
        assert j.mode is None  # falls through to manifest default at resolve time

    def test_job_can_override_mode(self):
        j = JobOverride.model_validate({"name": "m", "mode": "mobile"})
        assert j.mode == Mode.MOBILE

    def test_empty_name_rejected(self):
        with pytest.raises(Exception):
            JobOverride.model_validate({"name": ""})

    def test_unknown_field_rejected(self):
        # Forbids per-Job behavior/identity/post_visit until they're
        # actually wired up — fail early instead of silently ignoring.
        with pytest.raises(Exception):
            JobOverride.model_validate({"name": "x", "behavior": {}})


class TestManifestJobsField:
    def test_jobs_absent_means_single_implicit_job(self):
        m = Manifest.model_validate(_example_manifest_dict())
        assert m.jobs is None

    def test_jobs_explicit_list_parses(self):
        data = _example_manifest_dict()
        data["jobs"] = [
            {"name": "a", "mode": "desktop"},
            {"name": "b", "mode": "mobile"},
        ]
        m = Manifest.model_validate(data)
        assert m.jobs is not None
        assert [j.name for j in m.jobs] == ["a", "b"]

    def test_duplicate_job_names_rejected(self):
        data = _example_manifest_dict()
        data["jobs"] = [{"name": "x"}, {"name": "x"}]
        with pytest.raises(Exception):
            Manifest.model_validate(data)


class TestResolveJobs:
    def test_no_jobs_block_yields_one_default_job(self):
        m = Manifest.model_validate(_example_manifest_dict())
        resolved = resolve_jobs(m)
        assert len(resolved) == 1
        assert resolved[0] == ResolvedJob(name="default", mode=Mode.DESKTOP)

    def test_no_jobs_block_inherits_top_level_mode(self):
        data = _example_manifest_dict()
        data["mode"] = "mobile"
        m = Manifest.model_validate(data)
        resolved = resolve_jobs(m)
        assert resolved == [ResolvedJob(name="default", mode=Mode.MOBILE)]

    def test_job_inherits_top_level_mode_when_unspecified(self):
        data = _example_manifest_dict()
        data["mode"] = "mobile"
        data["jobs"] = [{"name": "a"}]  # no mode override
        m = Manifest.model_validate(data)
        resolved = resolve_jobs(m)
        assert resolved == [ResolvedJob(name="a", mode=Mode.MOBILE)]

    def test_job_mode_override_wins(self):
        data = _example_manifest_dict()
        data["mode"] = "desktop"
        data["jobs"] = [{"name": "m1", "mode": "mobile"}]
        m = Manifest.model_validate(data)
        resolved = resolve_jobs(m)
        assert resolved == [ResolvedJob(name="m1", mode=Mode.MOBILE)]

    def test_mixed_jobs_resolve_independently(self):
        data = _example_manifest_dict()
        data["jobs"] = [
            {"name": "d1", "mode": "desktop"},
            {"name": "d2"},                   # inherits desktop (top-level default)
            {"name": "m1", "mode": "mobile"},
        ]
        m = Manifest.model_validate(data)
        resolved = resolve_jobs(m)
        modes = [(r.name, r.mode) for r in resolved]
        assert modes == [
            ("d1", Mode.DESKTOP),
            ("d2", Mode.DESKTOP),
            ("m1", Mode.MOBILE),
        ]

    def test_resolved_job_is_immutable(self):
        # Frozen dataclass — protects the runner from accidental mutation
        # after resolution.
        r = ResolvedJob(name="x", mode=Mode.DESKTOP)
        with pytest.raises(Exception):
            r.mode = Mode.MOBILE  # type: ignore[misc]


# ────────────────────────────────────────────────
# persist_results — job/mode tagging + multi-Job grouping
# ────────────────────────────────────────────────

def _persist_manifest(tmp_path: Path, output_mode: str = "overwrite") -> Manifest:
    """A manifest pointed at a tmp_path so each test gets a clean output."""
    data = _example_manifest_dict()
    data["output"] = {"path": str(tmp_path / "out.yaml"), "mode": output_mode}
    data["targets"] = {
        "source": "inline",
        "items": [{"blog_id": "b1", "keyword": "k1", "title": "t1"}],
    }
    return Manifest.model_validate(data)


def _result(blog_id="b1", keyword="k1", title="t1", rank=5) -> RankResult:
    return RankResult(
        blog_id=blog_id, keyword=keyword, title=title,
        checked_at="2026-04-27T10:00:00+09:00",
        section="body", rank=rank, url=f"https://blog.naver.com/{blog_id}/1",
        reason="matched by blog_id in body", visit=None,
    )


class TestPersistResults:
    def test_run_dict_carries_job_and_mode_tags(self, tmp_path: Path):
        m = _persist_manifest(tmp_path)
        persist_results(m, [_result()], job_name="desktop-1", mode=Mode.DESKTOP)
        out = yaml.safe_load(Path(m.output.path).read_text(encoding="utf-8"))
        run = out[0]["runs"][0]
        assert run["job"] == "desktop-1"
        assert run["mode"] == "desktop"
        # job/mode appear before the rest so YAML reads naturally.
        keys = list(run.keys())
        assert keys[:2] == ["job", "mode"]

    def test_two_jobs_same_target_share_a_group_with_distinct_runs(self, tmp_path: Path):
        m = _persist_manifest(tmp_path, output_mode="append")
        # Job 1 (desktop) measures, then Job 2 (mobile) measures the same target.
        persist_results(m, [_result(rank=5)], job_name="d1", mode=Mode.DESKTOP)
        persist_results(m, [_result(rank=8)], job_name="m1", mode=Mode.MOBILE)
        out = yaml.safe_load(Path(m.output.path).read_text(encoding="utf-8"))
        # Single target group, two runs distinguishable by tags.
        assert len(out) == 1
        runs = out[0]["runs"]
        assert len(runs) == 2
        assert {r["job"] for r in runs} == {"d1", "m1"}
        assert {r["mode"] for r in runs} == {"desktop", "mobile"}
        # Ranks preserved per Job.
        by_job = {r["job"]: r["rank"] for r in runs}
        assert by_job == {"d1": 5, "m1": 8}

    def test_overwrite_mode_drops_existing_runs(self, tmp_path: Path):
        m = _persist_manifest(tmp_path, output_mode="append")
        persist_results(m, [_result(rank=5)], job_name="d1", mode=Mode.DESKTOP)
        # Switch to overwrite — prior run should be discarded.
        m_ow = _persist_manifest(tmp_path, output_mode="overwrite")
        # Reuse same output path
        m_ow = m_ow.model_copy(update={"output": m.output.model_copy(update={"mode": OutputMode.OVERWRITE})})
        persist_results(m_ow, [_result(rank=9)], job_name="d2", mode=Mode.DESKTOP)
        out = yaml.safe_load(Path(m.output.path).read_text(encoding="utf-8"))
        runs = out[0]["runs"]
        assert len(runs) == 1
        assert runs[0]["job"] == "d2"
        assert runs[0]["rank"] == 9

    def test_different_targets_become_separate_groups(self, tmp_path: Path):
        m = _persist_manifest(tmp_path, output_mode="append")
        persist_results(m, [
            _result(blog_id="b1", keyword="k1", title="t1"),
            _result(blog_id="b2", keyword="k2", title="t2"),
        ], job_name="d1", mode=Mode.DESKTOP)
        out = yaml.safe_load(Path(m.output.path).read_text(encoding="utf-8"))
        assert len(out) == 2
        ids = sorted(g["blog_id"] for g in out)
        assert ids == ["b1", "b2"]

    def test_append_only_preserves_earlier_results_under_overwrite_policy(self, tmp_path: Path):
        # Regression: when output.mode is overwrite and 2+ Jobs run within
        # the same scheduled run, only the first Job should honor overwrite.
        # Subsequent Jobs must pass append_only=True, otherwise they wipe
        # the earlier Job's persisted results.
        m = _persist_manifest(tmp_path, output_mode="overwrite")
        # First Job — append_only=False, honors overwrite (file effectively new).
        persist_results(m, [_result(rank=5)], job_name="d1", mode=Mode.DESKTOP)
        # Second Job in same run — must append, not clobber.
        persist_results(
            m, [_result(rank=8)], job_name="m1", mode=Mode.MOBILE,
            append_only=True,
        )
        out = yaml.safe_load(Path(m.output.path).read_text(encoding="utf-8"))
        runs = out[0]["runs"]
        assert len(runs) == 2
        assert {r["job"] for r in runs} == {"d1", "m1"}


# ────────────────────────────────────────────────
# Published-at gating — schema + due_status policy
# ────────────────────────────────────────────────

class TestPublishedAtSchema:
    def test_empty_published_at_is_accepted(self):
        item = TargetItem.model_validate(
            {"blog_id": "b", "keyword": "k", "title": "t", "published_at": ""},
        )
        assert item.published_at == ""

    def test_iso_with_timezone_offset_is_accepted(self):
        item = TargetItem.model_validate({
            "blog_id": "b", "keyword": "k", "title": "t",
            "published_at": "2026-04-27T15:00:00+09:00",
        })
        assert item.published_at == "2026-04-27T15:00:00+09:00"

    def test_iso_with_z_suffix_is_accepted(self):
        # Python 3.11+'s fromisoformat accepts trailing 'Z' for UTC.
        item = TargetItem.model_validate({
            "blog_id": "b", "keyword": "k", "title": "t",
            "published_at": "2026-04-27T06:00:00Z",
        })
        assert "Z" in item.published_at

    def test_naive_datetime_is_rejected(self):
        # No timezone offset is ambiguous — refuse instead of guessing.
        with pytest.raises(Exception, match="timezone"):
            TargetItem.model_validate({
                "blog_id": "b", "keyword": "k", "title": "t",
                "published_at": "2026-04-27T15:00:00",
            })

    def test_garbage_is_rejected(self):
        with pytest.raises(Exception):
            TargetItem.model_validate({
                "blog_id": "b", "keyword": "k", "title": "t",
                "published_at": "tomorrow morning",
            })


class TestLookupDelayDelta:
    def test_default_is_60min(self):
        t = Targets.model_validate({"source": "file", "path": "x.yaml"})
        assert t.lookup_delay_delta == timedelta(minutes=60)

    def test_zero_disables_gating(self):
        t = Targets.model_validate({
            "source": "file", "path": "x.yaml",
            "lookup_delay_after_published": "0s",
        })
        assert t.lookup_delay_delta == timedelta(0)

    def test_invalid_duration_rejected(self):
        with pytest.raises(Exception):
            Targets.model_validate({
                "source": "file", "path": "x.yaml",
                "lookup_delay_after_published": "soon",
            })


class TestDueStatus:
    def _query(self, published_at: str = "") -> RankQuery:
        return RankQuery(blog_id="b", keyword="k", title="t", published_at=published_at)

    def test_empty_published_at_is_always_due(self):
        now = datetime.now(timezone.utc)
        is_due, msg = _due_status(self._query(""), timedelta(minutes=60), now)
        assert is_due is True
        assert msg == ""

    def test_past_publish_with_delay_already_elapsed_is_due(self):
        # Published 2h ago, 60m delay → due 1h ago.
        now = datetime(2026, 4, 27, 15, 0, tzinfo=timezone.utc)
        pub = now - timedelta(hours=2)
        is_due, _ = _due_status(self._query(pub.isoformat()), timedelta(minutes=60), now)
        assert is_due is True

    def test_recent_publish_within_delay_is_NOT_due(self):
        # Published 30m ago, 60m delay → due 30m from now.
        now = datetime(2026, 4, 27, 15, 0, tzinfo=timezone.utc)
        pub = now - timedelta(minutes=30)
        is_due, msg = _due_status(self._query(pub.isoformat()), timedelta(minutes=60), now)
        assert is_due is False
        assert "in 30m" in msg

    def test_future_publish_is_NOT_due(self):
        # Published 2h from now, 60m delay → due 3h from now.
        now = datetime(2026, 4, 27, 15, 0, tzinfo=timezone.utc)
        pub = now + timedelta(hours=2)
        is_due, msg = _due_status(self._query(pub.isoformat()), timedelta(minutes=60), now)
        assert is_due is False
        assert "in 180m" in msg

    def test_exact_boundary_is_due(self):
        # now == published_at + delay → eligible.
        pub = datetime(2026, 4, 27, 15, 0, tzinfo=timezone.utc)
        delay = timedelta(minutes=60)
        now = pub + delay
        is_due, _ = _due_status(self._query(pub.isoformat()), delay, now)
        assert is_due is True

    def test_zero_delay_means_due_immediately_after_publish(self):
        now = datetime(2026, 4, 27, 15, 0, tzinfo=timezone.utc)
        pub = now - timedelta(seconds=1)
        is_due, _ = _due_status(self._query(pub.isoformat()), timedelta(0), now)
        assert is_due is True

    def test_remaining_minutes_floor_at_one(self):
        # Even ~30s remaining shouldn't show "in 0m" — that reads as "due now"
        # to a user. Always show at least 1 minute remaining.
        now = datetime(2026, 4, 27, 15, 0, tzinfo=timezone.utc)
        pub = now - timedelta(minutes=59, seconds=30)
        is_due, msg = _due_status(self._query(pub.isoformat()), timedelta(minutes=60), now)
        assert is_due is False
        assert "in 1m" in msg

    def test_schedule_start_immediate_passes_through(self):
        m = Manifest.model_validate(_example_manifest_dict())
        assert m.schedule.start == "immediate"

    def test_schedule_start_iso_with_offset_parses(self):
        data = _example_manifest_dict()
        data["schedule"]["start"] = "2026-04-27T04:00:00+09:00"
        m = Manifest.model_validate(data)
        assert isinstance(m.schedule.start, datetime)
        assert m.schedule.start.tzinfo is not None

    def test_schedule_start_naive_rejected(self):
        # Naive datetimes get silently compared against tz-aware "now" in
        # the runner — refuse instead of guessing the offset.
        data = _example_manifest_dict()
        data["schedule"]["start"] = "2026-04-27T04:00:00"
        with pytest.raises(Exception, match="timezone"):
            Manifest.model_validate(data)

    def test_schedule_start_garbage_rejected(self):
        data = _example_manifest_dict()
        data["schedule"]["start"] = "tomorrow morning"
        with pytest.raises(Exception):
            Manifest.model_validate(data)

    def test_handles_kst_offset(self):
        # Real-world publish times come with +09:00 — cross-tz comparison
        # must work without surprises.
        kst = timezone(timedelta(hours=9))
        pub_kst = datetime(2026, 4, 27, 15, 0, tzinfo=kst)  # 15:00 KST = 06:00 UTC
        # 30 min after publish in absolute terms — still inside the 60min gate.
        now_utc = datetime(2026, 4, 27, 6, 30, tzinfo=timezone.utc)
        is_due, _ = _due_status(self._query(pub_kst.isoformat()), timedelta(minutes=60), now_utc)
        assert is_due is False


# ────────────────────────────────────────────────
# Resources — schema, tracker detection, route handler
# ────────────────────────────────────────────────

class TestResourcesSchema:
    def test_default_block_list_is_empty(self):
        r = Resources.model_validate({})
        assert r.block == []
        assert r.block_third_party_trackers is False

    def test_block_accepts_known_resource_types(self):
        r = Resources.model_validate({"block": ["image", "font", "media", "stylesheet"]})
        assert ResourceType.IMAGE in r.block
        assert ResourceType.STYLESHEET in r.block

    def test_unknown_resource_type_rejected(self):
        # ``script`` and ``xhr`` are intentionally NOT exposed — blocking
        # them breaks rank parsing / Naver dynamic search variants.
        with pytest.raises(Exception):
            Resources.model_validate({"block": ["script"]})
        with pytest.raises(Exception):
            Resources.model_validate({"block": ["xhr"]})

    def test_unknown_field_rejected(self):
        with pytest.raises(Exception):
            Resources.model_validate({"block": [], "magic_setting": True})

    def test_manifest_resources_field_optional(self):
        m = Manifest.model_validate(_example_manifest_dict())
        assert m.resources is None

    def test_manifest_with_resources_block(self):
        data = _example_manifest_dict()
        data["resources"] = {
            "block": ["image", "font"],
            "block_third_party_trackers": True,
        }
        m = Manifest.model_validate(data)
        assert m.resources is not None
        assert ResourceType.IMAGE in m.resources.block
        assert m.resources.block_third_party_trackers is True


class TestIsTrackerHost:
    def test_known_trackers_match(self):
        assert is_tracker_host("https://www.googletagmanager.com/gtm.js") is True
        assert is_tracker_host("https://stats.g.doubleclick.net/dc.js") is True
        assert is_tracker_host("https://connect.facebook.net/en_US/fbevents.js") is True
        assert is_tracker_host("https://b.scorecardresearch.com/p?c1=2") is True

    def test_naver_first_party_does_not_match(self):
        # Critical — we must never block naver.* even if naming overlaps.
        assert is_tracker_host("https://search.naver.com/search.naver") is False
        assert is_tracker_host("https://siape.veta.naver.com/log") is False
        assert is_tracker_host("https://ssl.pstatic.net/static/blog/x.js") is False

    def test_unknown_host_does_not_match(self):
        assert is_tracker_host("https://example.com/whatever") is False

    def test_malformed_url_does_not_crash(self):
        # urlparse is forgiving; absent host returns None which we coerce to "".
        assert is_tracker_host("not-a-url") is False
        assert is_tracker_host("") is False


class _FakeRequest:
    def __init__(self, url: str, resource_type: str) -> None:
        self.url = url
        self.resource_type = resource_type


class _FakeRoute:
    def __init__(self) -> None:
        self.action: str | None = None  # "abort" | "continue"

    async def abort(self) -> None:
        self.action = "abort"

    async def continue_(self) -> None:
        self.action = "continue"


class TestRouteHandler:
    @pytest.mark.asyncio
    async def test_blocks_listed_resource_type(self):
        counter = BlockCounter()
        resources = Resources.model_validate({"block": ["image"]})
        handler = make_route_handler(resources, counter)

        route, req = _FakeRoute(), _FakeRequest("https://x/y.jpg", "image")
        await handler(route, req)
        assert route.action == "abort"
        assert counter.blocked == 1
        assert counter.allowed == 0

    @pytest.mark.asyncio
    async def test_passes_unlisted_resource_type(self):
        counter = BlockCounter()
        resources = Resources.model_validate({"block": ["image"]})
        handler = make_route_handler(resources, counter)

        route, req = _FakeRoute(), _FakeRequest("https://x/y.html", "document")
        await handler(route, req)
        assert route.action == "continue"
        assert counter.allowed == 1
        assert counter.blocked == 0

    @pytest.mark.asyncio
    async def test_blocks_tracker_when_enabled(self):
        counter = BlockCounter()
        resources = Resources.model_validate({
            "block": [], "block_third_party_trackers": True,
        })
        handler = make_route_handler(resources, counter)

        route = _FakeRoute()
        # Even if the resource type is "script" (which we don't block by
        # type), tracker host should still trip the abort.
        req = _FakeRequest("https://www.googletagmanager.com/gtm.js", "script")
        await handler(route, req)
        assert route.action == "abort"
        assert counter.blocked == 1

    @pytest.mark.asyncio
    async def test_passes_tracker_when_disabled(self):
        counter = BlockCounter()
        resources = Resources.model_validate({
            "block": [], "block_third_party_trackers": False,
        })
        handler = make_route_handler(resources, counter)

        route = _FakeRoute()
        req = _FakeRequest("https://www.googletagmanager.com/gtm.js", "script")
        await handler(route, req)
        assert route.action == "continue"
        assert counter.allowed == 1

    @pytest.mark.asyncio
    async def test_naver_first_party_passes_even_with_trackers_blocked(self):
        # Critical — make sure the tracker filter never accidentally
        # catches naver hosts.
        counter = BlockCounter()
        resources = Resources.model_validate({
            "block": ["image"], "block_third_party_trackers": True,
        })
        handler = make_route_handler(resources, counter)

        route = _FakeRoute()
        req = _FakeRequest("https://siape.veta.naver.com/log", "xhr")
        await handler(route, req)
        assert route.action == "continue"


class TestBlockCounterSummary:
    def test_empty_returns_blank_string(self):
        assert BlockCounter().summary() == ""

    def test_summary_format(self):
        c = BlockCounter(allowed=70, blocked=30)
        s = c.summary()
        assert "30" in s
        assert "100" in s
        assert "30%" in s


# ────────────────────────────────────────────────
# Cache schema
# ────────────────────────────────────────────────

class TestCacheSchema:
    def test_default_is_disabled(self):
        c = Cache.model_validate({})
        assert c.enabled is False
        assert c.domains == ["pstatic.net"]
        assert c.min_ttl == "1m"
        assert c.max_ttl == "24h"

    def test_enabled_with_custom_domains(self):
        c = Cache.model_validate({
            "enabled": True,
            "domains": ["pstatic.net", "example.cdn"],
        })
        assert c.enabled is True
        assert "example.cdn" in c.domains

    def test_empty_domains_rejected(self):
        # Empty whitelist would be useless — would never match, just
        # spending CPU on the check. Schema refuses so misconfiguration
        # surfaces at load time, not silently as zero hits.
        with pytest.raises(Exception):
            Cache.model_validate({"enabled": True, "domains": []})

    def test_inverted_ttl_range_rejected(self):
        with pytest.raises(Exception, match="min_ttl"):
            Cache.model_validate({"min_ttl": "2h", "max_ttl": "1h"})

    def test_garbage_ttl_rejected(self):
        with pytest.raises(Exception):
            Cache.model_validate({"min_ttl": "soon"})

    def test_unknown_field_rejected(self):
        with pytest.raises(Exception):
            Cache.model_validate({"enabled": True, "magic": 1})

    def test_ttl_seconds_properties(self):
        c = Cache.model_validate({"min_ttl": "30s", "max_ttl": "2h"})
        assert c.min_ttl_seconds == 30
        assert c.max_ttl_seconds == 7200

    def test_resources_default_has_no_cache(self):
        r = Resources.model_validate({})
        assert r.cache is None

    def test_resources_with_cache_block(self):
        r = Resources.model_validate({
            "block": ["image"],
            "cache": {"enabled": True},
        })
        assert r.cache is not None
        assert r.cache.enabled is True


# ────────────────────────────────────────────────
# DiskCache — request/response gates
# ────────────────────────────────────────────────

class _FakeCacheRequest:
    def __init__(self, url: str, resource_type: str = "script", method: str = "GET"):
        self.url = url
        self.resource_type = resource_type
        self.method = method


class TestIsCacheableRequest:
    def setup_method(self):
        self.domains = frozenset({"pstatic.net"})

    def test_get_script_on_whitelisted_domain_passes(self):
        req = _FakeCacheRequest("https://ssl.pstatic.net/static/blog/x.js")
        assert _is_cacheable_request(req, self.domains) is True

    def test_post_rejected_even_if_otherwise_cacheable(self):
        req = _FakeCacheRequest(
            "https://ssl.pstatic.net/x.js", method="POST",
        )
        assert _is_cacheable_request(req, self.domains) is False

    def test_non_script_resource_rejected(self):
        # XHR / document / fetch must always be fresh — they carry rank
        # data we cannot serve from cache.
        for rt in ("xhr", "fetch", "document", "image", "stylesheet"):
            req = _FakeCacheRequest(
                "https://ssl.pstatic.net/x.js", resource_type=rt,
            )
            assert _is_cacheable_request(req, self.domains) is False, rt

    def test_offlist_domain_rejected(self):
        req = _FakeCacheRequest("https://search.naver.com/main.js")
        assert _is_cacheable_request(req, self.domains) is False


class TestIsCacheableResponse:
    def test_basic_max_age_is_cacheable(self):
        ok, ttl = _is_cacheable_response({"cache-control": "public, max-age=600"})
        assert ok is True
        assert ttl == 600

    def test_no_cache_control_header_is_not_cacheable(self):
        ok, ttl = _is_cacheable_response({"content-type": "application/javascript"})
        assert ok is False
        assert ttl == 0

    def test_no_store_refused(self):
        ok, _ = _is_cacheable_response({"cache-control": "no-store, max-age=600"})
        assert ok is False

    def test_no_cache_refused(self):
        ok, _ = _is_cacheable_response({"cache-control": "no-cache, max-age=600"})
        assert ok is False

    def test_private_refused(self):
        # Private = user-specific; sharing across Jobs (= sharing across
        # IPs) is exactly what we promise NOT to do.
        ok, _ = _is_cacheable_response({"cache-control": "private, max-age=600"})
        assert ok is False

    def test_must_revalidate_refused(self):
        ok, _ = _is_cacheable_response({
            "cache-control": "max-age=600, must-revalidate",
        })
        assert ok is False

    def test_set_cookie_refused(self):
        # Set-Cookie marks the response as personalized — even with
        # max-age, we cannot share it.
        ok, _ = _is_cacheable_response({
            "cache-control": "max-age=600",
            "set-cookie": "session=abc",
        })
        assert ok is False

    def test_vary_accept_encoding_only_is_ok(self):
        ok, ttl = _is_cacheable_response({
            "cache-control": "max-age=600",
            "vary": "Accept-Encoding",
        })
        assert ok is True
        assert ttl == 600

    def test_vary_other_header_refused(self):
        # We don't key on cookies / user-agent — refuse responses that
        # demand we do.
        ok, _ = _is_cacheable_response({
            "cache-control": "max-age=600",
            "vary": "Cookie",
        })
        assert ok is False

    def test_vary_with_extra_dimension_refused(self):
        ok, _ = _is_cacheable_response({
            "cache-control": "max-age=600",
            "vary": "Accept-Encoding, User-Agent",
        })
        assert ok is False

    def test_header_lookup_is_case_insensitive(self):
        ok, ttl = _is_cacheable_response({
            "Cache-Control": "max-age=600",
            "Set-Cookie": "session=abc",
        })
        assert ok is False


# ────────────────────────────────────────────────
# DiskCache — round-trip, TTL, soft fail
# ────────────────────────────────────────────────

def _build_cache(tmp_path: Path, **overrides) -> DiskCache:
    args = dict(
        root=tmp_path / "cache",
        domains=["pstatic.net"],
        min_ttl_seconds=60,
        max_ttl_seconds=3600,
        max_body_bytes=1024 * 1024,
    )
    args.update(overrides)
    return DiskCache(**args)


class TestDiskCacheRoundTrip:
    def test_put_then_get_returns_body(self, tmp_path: Path):
        cache = _build_cache(tmp_path)
        url = "https://ssl.pstatic.net/x.js"
        body = b"console.log('hi');"
        assert cache.put(url, body, ttl_seconds=600, content_type="application/javascript") is True
        hit = cache.get(url)
        assert hit is not None
        got_body, content_type = hit
        assert got_body == body
        assert content_type == "application/javascript"

    def test_get_on_cold_cache_returns_none(self, tmp_path: Path):
        cache = _build_cache(tmp_path)
        assert cache.get("https://ssl.pstatic.net/missing.js") is None

    def test_has_fresh_matches_get(self, tmp_path: Path):
        cache = _build_cache(tmp_path)
        url = "https://ssl.pstatic.net/x.js"
        assert cache.has_fresh(url) is False
        cache.put(url, b"x", ttl_seconds=600, content_type="application/javascript")
        assert cache.has_fresh(url) is True

    def test_distinct_urls_dont_collide(self, tmp_path: Path):
        cache = _build_cache(tmp_path)
        cache.put("https://x/a.js", b"AAA", 600, "application/javascript")
        cache.put("https://x/b.js", b"BBB", 600, "application/javascript")
        assert cache.get("https://x/a.js")[0] == b"AAA"
        assert cache.get("https://x/b.js")[0] == b"BBB"

    def test_put_idempotent_for_same_url(self, tmp_path: Path):
        # Two writers racing on the same URL is the documented "fine"
        # path — content is identical, last writer wins.
        cache = _build_cache(tmp_path)
        url = "https://x/a.js"
        cache.put(url, b"v1", 600, "application/javascript")
        cache.put(url, b"v1", 600, "application/javascript")
        assert cache.get(url)[0] == b"v1"


class TestDiskCacheTtl:
    def test_below_min_ttl_is_skipped(self, tmp_path: Path):
        # Server says 1s, our floor is 60s — refuse to store rather than
        # silently extend a deliberately-short TTL.
        cache = _build_cache(tmp_path, min_ttl_seconds=60)
        stored = cache.put(
            "https://x/a.js", b"x",
            ttl_seconds=1, content_type="application/javascript",
        )
        assert stored is False
        assert cache.get("https://x/a.js") is None

    def test_max_ttl_caps_long_max_age(self, tmp_path: Path, monkeypatch):
        # Server says max-age=year; we cap at max_ttl. After max_ttl+1
        # seconds (simulated via monkeypatched time.time), the entry must
        # be gone — proving the cap wrote a near-term expiry, not the
        # year the server requested.
        import ranker.resources as res_mod

        cache = _build_cache(tmp_path, min_ttl_seconds=1, max_ttl_seconds=10)
        cache.put("https://x/a.js", b"x", ttl_seconds=31_536_000, content_type="application/javascript")

        real_time = res_mod.time.time
        monkeypatch.setattr(
            res_mod.time, "time", lambda: real_time() + 11,
        )
        assert cache.get("https://x/a.js") is None
        assert cache.has_fresh("https://x/a.js") is False

    def test_expired_entry_returns_none(self, tmp_path: Path, monkeypatch):
        import ranker.resources as res_mod

        cache = _build_cache(tmp_path, min_ttl_seconds=1, max_ttl_seconds=10)
        cache.put("https://x/a.js", b"x", ttl_seconds=5, content_type="application/javascript")
        real_time = res_mod.time.time
        monkeypatch.setattr(res_mod.time, "time", lambda: real_time() + 6)
        assert cache.get("https://x/a.js") is None


class TestDiskCacheSoftFail:
    def test_corrupt_meta_returns_none_not_raises(self, tmp_path: Path):
        cache = _build_cache(tmp_path)
        url = "https://x/a.js"
        cache.put(url, b"x", 600, "application/javascript")
        _, meta_path = cache._paths(url)
        meta_path.write_text("not-json{{{")
        # Must NOT raise — caching is best-effort.
        assert cache.get(url) is None
        assert cache.has_fresh(url) is False

    def test_missing_body_returns_none(self, tmp_path: Path):
        cache = _build_cache(tmp_path)
        url = "https://x/a.js"
        cache.put(url, b"x", 600, "application/javascript")
        body_path, _ = cache._paths(url)
        body_path.unlink()
        assert cache.get(url) is None

    def test_oversize_body_skipped(self, tmp_path: Path):
        cache = _build_cache(tmp_path, max_body_bytes=10)
        stored = cache.put(
            "https://x/big.js", b"x" * 100, 600, "application/javascript",
        )
        assert stored is False
        assert cache.get("https://x/big.js") is None


class TestCacheCounterSummary:
    def test_empty_returns_blank_string(self):
        assert CacheCounter().summary() == ""

    def test_summary_includes_hits_misses_and_bytes(self):
        c = CacheCounter(hits=8, misses=2, stored=2, bytes_saved=4096)
        s = c.summary()
        assert "8" in s   # hits
        assert "10" in s  # total looked up
        assert "2" in s   # stored
        assert "4" in s   # KB
