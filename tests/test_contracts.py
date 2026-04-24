"""Contract tests — the manifest schema and the pure matching logic.

Network / Playwright behavior is covered separately; here we prove the
declarative surface and the rank-matching policy are correct in isolation.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ranker.event import RankQuery
from ranker.manifest import (
    MatchBy,
    Manifest,
    OutputMode,
    RotatePolicy,
    TargetSourceKind,
    load_manifest,
    parse_duration,
)
from ranker.search import extract_blog_id, match_rank


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
            "locator": {"type": "css", "value": "div.spw_rerank._slog_visible"},
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
        assert m.behavior.dwell_ms.min == 4000

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
