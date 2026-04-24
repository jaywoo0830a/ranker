"""Contract tests — the manifest schema and the pure matching logic.

Network / Playwright behavior is covered separately; here we prove the
declarative surface and the rank-matching policy are correct in isolation.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ranker.event import RankQuery
from ranker.event import Engagement, RankResult, VisitResult
from ranker.manifest import (
    MatchBy,
    Manifest,
    OutputMode,
    RotatePolicy,
    TargetSourceKind,
    load_manifest,
    parse_duration,
)
from ranker.runner import _result_to_run_dict
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
