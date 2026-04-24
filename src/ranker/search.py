"""Naver unified-search automation and blog_id rank matching."""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from playwright.async_api import BrowserContext, TimeoutError as PWTimeoutError

from .behavior import Human
from .event import RankQuery, RankResult
from .manifest import LocatorType, MatchBy, Matching, Source


_BLOG_ID_PATTERNS = (
    re.compile(r"https?://(?:m\.)?blog\.naver\.com/([^/?#]+)/"),
    re.compile(r"[?&]blogId=([^&#]+)"),
)


def extract_blog_id(url: str) -> str | None:
    """Pull a Naver blog_id out of a post URL, whichever form it takes."""
    for pattern in _BLOG_ID_PATTERNS:
        match = pattern.search(url)
        if match:
            return match.group(1)
    return None


def match_rank(items: list[dict[str, str]], query: RankQuery, by: MatchBy) -> tuple[int | None, str]:
    """Scan already-extracted items in block order, return (1-indexed rank, reason).

    Each item is ``{"url": ..., "title": ...}``. Separating this from
    Playwright keeps the matching policy unit-testable.
    """
    for idx, item in enumerate(items, start=1):
        url = item.get("url", "")
        title = item.get("title", "")
        blog_id = extract_blog_id(url)

        if by == MatchBy.BLOG_ID and blog_id == query.blog_id:
            return idx, "matched by blog_id"
        if by == MatchBy.TITLE and title.strip() == query.title.strip():
            return idx, "matched by title"
        if by == MatchBy.BLOG_ID_AND_TITLE:
            if blog_id == query.blog_id and title.strip() == query.title.strip():
                return idx, "matched by blog_id and title"

    return None, f"not found in top {len(items)}"


def _kst_now_iso() -> str:
    return datetime.now(ZoneInfo("Asia/Seoul")).isoformat(timespec="seconds")


def _locator_string(source: Source) -> str:
    """Translate the source.locator DSL into a Playwright selector string."""
    if source.locator.type == LocatorType.CSS:
        return source.locator.value
    return f"xpath={source.locator.value}"


class NaverSearch:
    """Drives one Playwright context through keyword → rank for a target."""

    def __init__(self, source: Source, matching: Matching, human: Human) -> None:
        self._source = source
        self._matching = matching
        self._human = human

    async def look_up(self, context: BrowserContext, query: RankQuery) -> RankResult:
        page = await context.new_page()
        try:
            await page.goto("https://www.naver.com/", wait_until="domcontentloaded")
            await self._human.type_like_human(page, "input#query", query.keyword)
            await page.keyboard.press("Enter")

            block_selector = _locator_string(self._source)
            try:
                await page.wait_for_selector(block_selector, timeout=10_000)
            except PWTimeoutError:
                return RankResult(
                    blog_id=query.blog_id,
                    keyword=query.keyword,
                    title=query.title,
                    checked_at=_kst_now_iso(),
                    rank=None,
                    reason="result block not found",
                )

            items = await self._extract_items(page, block_selector)
            rank, reason = match_rank(items, query, self._matching.by)

            await self._human.dwell_with_activity(page)

            return RankResult(
                blog_id=query.blog_id,
                keyword=query.keyword,
                title=query.title,
                checked_at=_kst_now_iso(),
                rank=rank,
                reason=reason,
            )
        finally:
            await page.close()

    async def _extract_items(self, page, block_selector: str) -> list[dict[str, str]]:
        """Pull ordered ``{url, title}`` records from the result block."""
        return await page.evaluate(
            """({block, depth}) => {
                const root = document.querySelector(block);
                if (!root) return [];
                const anchors = Array.from(root.querySelectorAll('a[href]'));
                const seen = new Set();
                const items = [];
                for (const a of anchors) {
                    const href = a.href || '';
                    if (!href || seen.has(href)) continue;
                    const text = (a.innerText || a.textContent || '').trim();
                    if (!text) continue;
                    seen.add(href);
                    items.push({url: href, title: text});
                    if (items.length >= depth) break;
                }
                return items;
            }""",
            {"block": block_selector, "depth": self._source.scan_depth},
        )
