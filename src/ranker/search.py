"""Naver unified-search automation and blog_id rank matching.

This module owns the Naver-specific pieces: the result-block anchor DOM
shape, the blog_id URL patterns, and the iframe detection rules for blog
posts. Generic Playwright plumbing lives in :mod:`ranker.browser`.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.async_api import BrowserContext, Frame, Page

from .behavior import Human
from .browser import (
    first_visible_bounding_box,
    goto_stable,
    poll_until,
    wait_for_any,
)
from .event import RankQuery, RankResult, VisitResult
from .manifest import IntRange, Locator, LocatorType, MatchBy, Matching, ScrollPolicy, Source
from .profile import ModeProfile


_BLOG_ID_PATTERNS = (
    re.compile(r"https?://(?:m\.)?blog\.naver\.com/([^/?#]+)/"),
    re.compile(r"[?&]blogId=([^&#]+)"),
)
_LOG_NO_IN_URL = re.compile(r"/\d+(?:$|[?#])")
_CONTENT_IFRAME_SELECTORS = ("iframe#mainFrame", "iframe")
_MIN_CONTENT_IFRAME_DIM = 200
_MIN_SCROLL_HEIGHT = 1_500  # what counts as "the post body is loaded"


def extract_blog_id(url: str) -> str | None:
    """Pull a Naver blog_id out of a post URL, whichever form it takes."""
    for pattern in _BLOG_ID_PATTERNS:
        match = pattern.search(url)
        if match:
            return match.group(1)
    return None


def normalize_items(raw: list[dict[str, str]], scan_depth: int) -> list[dict[str, str]]:
    """Turn a raw anchor list into ordered, unique blog slots.

    Naver's result block contains navigation noise (Keep save, "바로가기")
    and a separate anchor per blog for the profile vs the post. This
    collapses them so one blog occupies one rank slot. When both a profile
    anchor and a post anchor exist for the same blog, we prefer the post
    anchor (so title-based matching keeps working).
    """
    best: dict[str, dict[str, str]] = {}
    order: list[str] = []

    for item in raw:
        url = item.get("url", "")
        blog_id = extract_blog_id(url)
        if not blog_id:
            continue  # Keep / cafe / navigation — ignore
        has_post = bool(_LOG_NO_IN_URL.search(url))
        title = item.get("title", "").strip()
        entry = {"blog_id": blog_id, "url": url, "title": title, "is_post": has_post}

        existing = best.get(blog_id)
        if existing is None:
            best[blog_id] = entry
            order.append(blog_id)
            continue
        # Upgrade to the "post" anchor when we had only profile before.
        if entry["is_post"] and not existing["is_post"]:
            best[blog_id] = entry
        # Tie-break: prefer the anchor that actually carries a title.
        elif entry["is_post"] == existing["is_post"] and entry["title"] and not existing["title"]:
            best[blog_id] = entry

    slots = [best[bid] for bid in order][:scan_depth]
    return slots


def match_rank(slots: list[dict[str, str]], query: RankQuery, by: MatchBy) -> tuple[int | None, str]:
    """Scan normalized slots in order, return (1-indexed rank, reason)."""
    for idx, slot in enumerate(slots, start=1):
        blog_id = slot.get("blog_id") or extract_blog_id(slot.get("url", ""))
        title = slot.get("title", "")

        if by == MatchBy.BLOG_ID and blog_id == query.blog_id:
            return idx, "matched by blog_id"
        if by == MatchBy.TITLE and title.strip() == query.title.strip():
            return idx, "matched by title"
        if by == MatchBy.BLOG_ID_AND_TITLE:
            if blog_id == query.blog_id and title.strip() == query.title.strip():
                return idx, "matched by blog_id and title"

    return None, f"not found in top {len(slots)}"


def _kst_now_iso() -> str:
    return datetime.now(ZoneInfo("Asia/Seoul")).isoformat(timespec="seconds")


def _locator_string(locator: Locator) -> str:
    """Translate a Locator DSL into a Playwright selector string."""
    if locator.type == LocatorType.CSS:
        return locator.value
    return f"xpath={locator.value}"


def _find_content_frame(page: Page) -> Frame | None:
    """Return the Playwright Frame hosting the Naver blog post's content.

    Matches Naver's canonical ``mainFrame`` name, then falls back to any
    frame whose URL looks like a post view. Returns None on ordinary
    pages so callers use the main document for hover targets etc.
    """
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        if frame.name == "mainFrame":
            return frame
        if "blog.naver.com" in frame.url and "PostView" in frame.url:
            return frame
    return None


async def _find_content_area(page: Page) -> dict | None:
    """Bounding box of the blog post's content iframe, or None.

    Prefers ``iframe#mainFrame`` (Naver blog's canonical container) and
    falls back to the first visible iframe large enough to be content
    (not a tracking pixel or tiny ad).
    """
    for selector in _CONTENT_IFRAME_SELECTORS:
        box = await first_visible_bounding_box(
            page, selector, timeout_ms=3_000, min_dim=_MIN_CONTENT_IFRAME_DIM,
        )
        if box is not None:
            return box
    return None


async def _content_scroll_height(page: Page) -> int | None:
    """Scrollable pixel height of the post content.

    Reads from the iframe's inner document when present (Naver blog),
    otherwise from the outer document. Returns None when the page has no
    meaningful scroll length — callers fall back to viewport defaults.
    """
    height = await page.evaluate(
        """() => {
            const iframe = document.querySelector('iframe#mainFrame')
                        || document.querySelector('iframe');
            if (iframe) {
                try {
                    const doc = iframe.contentDocument;
                    if (doc && doc.body) return doc.body.scrollHeight;
                } catch (e) { /* cross-origin — fall through */ }
            }
            return document.body.scrollHeight;
        }"""
    )
    try:
        value = int(height)
        return value if value > 0 else None
    except (TypeError, ValueError):
        return None


async def _extract_anchors(page: Page, block_selector: str) -> list[dict[str, str]]:
    """Pull every ordered ``{url, title}`` anchor from the given block.

    Dedup/filtering happens in ``normalize_items`` so this stays a dumb
    DOM reader.
    """
    return await page.evaluate(
        """(block) => {
            const root = document.querySelector(block);
            if (!root) return [];
            const items = [];
            const seen = new Set();
            for (const a of root.querySelectorAll('a[href]')) {
                const href = a.href || '';
                if (!href || seen.has(href)) continue;
                const text = (a.innerText || a.textContent || '').trim();
                seen.add(href);
                items.push({url: href, title: text});
            }
            return items;
        }""",
        block_selector,
    )


class NaverSearch:
    """Drives one Playwright context through keyword → rank for a target.

    Naver renders two blocks in unified search: a highlighted ``head`` and a
    main ``body``. We check ``head`` first (more prominent) and fall through
    to ``body``; the reported ``section`` says which one matched.
    """

    def __init__(
        self,
        source: Source,
        matching: Matching,
        human: Human,
        profile: ModeProfile,
    ) -> None:
        self._source = source
        self._matching = matching
        self._human = human
        self._profile = profile
        # Sections default to the profile's preset when the manifest omits
        # them — supports ``mode: mobile`` with no selector boilerplate.
        self._sections = source.sections or profile.default_sections

    async def look_up(self, context: BrowserContext, query: RankQuery) -> RankResult:
        page = await context.new_page()
        try:
            # Skip the homepage → type → submit dance. We navigate straight
            # to Naver's integrated-search URL, which is where the typed
            # form submits anyway. This keeps the flow robust across Naver
            # UI changes and avoids the mobile overlay's fragile JS.
            url = self._profile.search_url_template.format(
                query=urllib.parse.quote(query.keyword),
            )
            await goto_stable(page, url)

            head_sel = _locator_string(self._sections.head)
            body_sel = _locator_string(self._sections.body)
            if not await wait_for_any(page, (head_sel, body_sel), timeout_ms=10_000):
                return self._miss(query, "result block not found")

            head_raw = await _extract_anchors(page, head_sel)
            body_raw = await _extract_anchors(page, body_sel)
            head_slots = normalize_items(head_raw, self._source.scan_depth)
            body_slots = normalize_items(body_raw, self._source.scan_depth)
            await self._debug_dump(page, query, head_raw, body_raw, head_slots, body_slots)

            head_rank, head_reason = match_rank(head_slots, query, self._matching.by)
            if head_rank is not None:
                matched = head_slots[head_rank - 1]
                return self._hit(query, "head", head_rank, matched["url"], head_reason + " in head")

            body_rank, body_reason = match_rank(body_slots, query, self._matching.by)
            if body_rank is not None:
                matched = body_slots[body_rank - 1]
                return self._hit(query, "body", body_rank, matched["url"], body_reason + " in body")

            return self._miss(
                query,
                f"not found in head (top {len(head_slots)} unique blogs from {len(head_raw)} anchors) "
                f"or body (top {len(body_slots)} unique blogs from {len(body_raw)} anchors)",
            )
        finally:
            await page.close()

    async def visit_post(
        self,
        context: BrowserContext,
        url: str,
        dwell_ms: IntRange,
        mouse_events: IntRange,
        scroll: ScrollPolicy,
    ) -> VisitResult:
        """Navigate to ``url`` in a fresh tab, dwell with mouse/scroll
        activity shaped by ``scroll``, and report back.

        Naver blog posts render their content inside ``iframe#mainFrame``.
        We feed the iframe's bounding box *and* inner scrollHeight to
        ``dwell`` so wheel events land on the inner document (Chromium
        routes wheel to the iframe when the cursor is over it) and so the
        per-nudge step targets the chosen reading depth.

        ``VisitResult`` is deliberately small (visited_at / url / dwelled_ms).
        The ``engagement`` field is reserved for a future extractor — when it
        lands it will populate the same record, so downstream consumers
        don't need to change.
        """
        page = await context.new_page()
        try:
            await goto_stable(page, url)
            # Naver lazy-loads the iframe body; without this extra wait we
            # dispatch wheel events into an iframe whose documentElement
            # hasn't grown past the viewport yet — nothing scrolls.
            scroll_height = await poll_until(
                lambda: _content_scroll_height(page),
                predicate=lambda h: h is not None and h >= _MIN_SCROLL_HEIGHT,
                timeout_ms=5_000,
            )
            area = await _find_content_area(page)
            content_frame = _find_content_frame(page)
            hover_offset = (
                (int(area["x"]), int(area["y"])) if area is not None else (0, 0)
            )
            dwelled = await self._human.dwell(
                page, dwell_ms, mouse_events, scroll,
                area=area, scroll_height=scroll_height,
                hover_frame=content_frame, hover_offset=hover_offset,
            )
            return VisitResult(
                visited_at=_kst_now_iso(),
                url=url,
                dwelled_ms=dwelled,
                engagement=None,
            )
        finally:
            await page.close()

    def _hit(self, query: RankQuery, section: str, rank: int, url: str, reason: str) -> RankResult:
        return RankResult(
            blog_id=query.blog_id,
            keyword=query.keyword,
            title=query.title,
            checked_at=_kst_now_iso(),
            section=section,
            rank=rank,
            url=url,
            reason=reason,
        )

    def _miss(self, query: RankQuery, reason: str) -> RankResult:
        return RankResult(
            blog_id=query.blog_id,
            keyword=query.keyword,
            title=query.title,
            checked_at=_kst_now_iso(),
            section=None,
            rank=None,
            url=None,
            reason=reason,
        )

    async def _debug_dump(
        self,
        page: Page,
        query: RankQuery,
        head_raw: list[dict[str, str]],
        body_raw: list[dict[str, str]],
        head_slots: list[dict[str, str]],
        body_slots: list[dict[str, str]],
    ) -> None:
        """Dump raw anchors + normalized slots + page HTML when RANKER_DEBUG=1.

        Intended for diagnosing "why didn't we match?" — the JSON makes it
        obvious which blogs Naver showed and at which slot our blog (if
        present) landed.
        """
        if os.environ.get("RANKER_DEBUG", "").lower() not in ("1", "true", "yes"):
            return
        out_dir = Path(os.environ.get("RANKER_DEBUG_DIR", "debug"))
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = _kst_now_iso().replace(":", "").replace("+", "p")
        safe_blog = re.sub(r"[^\w-]+", "_", query.blog_id)
        base = out_dir / f"{stamp}_{safe_blog}"
        base.with_suffix(".json").write_text(
            json.dumps(
                {
                    "query": {
                        "blog_id": query.blog_id,
                        "keyword": query.keyword,
                        "title": query.title,
                    },
                    "url": page.url,
                    "head_raw": head_raw,
                    "body_raw": body_raw,
                    "head_slots": head_slots,
                    "body_slots": body_slots,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        base.with_suffix(".html").write_text(await page.content(), encoding="utf-8")
