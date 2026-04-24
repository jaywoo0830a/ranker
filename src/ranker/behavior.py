"""Human-like interaction helpers layered on top of Playwright.

These helpers encode the defense principles from the reference material:
behavior signals (dwell, mouse events) must be present and plausible, and
timing must vary. All randomness flows through an injected ``random.Random``
so tests can pin a seed.
"""

from __future__ import annotations

import asyncio
import random

from playwright.async_api import Frame, Page

from .browser import query_element_centers
from .manifest import Behavior, FloatRange, IntRange, ScrollPolicy


# Neutral scroll policy for search-page dwell: brief skim (10–30% depth),
# no directional bias. Only exists to leave behavior-layer signals — the
# search result page isn't actually being "read".
_NEUTRAL_SCROLL = ScrollPolicy(
    depth_ratio=FloatRange(min=0.1, max=0.3),
    down_bias=0.5,
)

# Fraction of mouse_events that are wheel scrolls (rest are cursor moves,
# with hovers mixed in). Tuned for a reading feel.
_SCROLL_EVENT_FRACTION = 0.75

# Among non-scroll events, fraction that are element hovers (rest are
# random cursor moves). Hovers fire mouseover/mouseenter, which engagement
# trackers look for alongside scrolls.
_HOVER_EVENT_FRACTION = 0.5

# CSS selector for elements worth hovering — links, buttons, images,
# headings, and content blocks. Kept loose so it works on any page.
_HOVER_SELECTOR = "a, button, img, h1, h2, h3, li, p"


class Human:
    """Stateful helper that applies a Behavior policy to a Playwright Page."""

    def __init__(self, behavior: Behavior, rng: random.Random | None = None) -> None:
        self._b = behavior
        self._rng = rng or random.Random()

    def _randint(self, lo: int, hi: int) -> int:
        return self._rng.randint(lo, hi) if lo < hi else lo

    async def type_like_human(self, page: Page, selector: str, text: str) -> None:
        """Type ``text`` one character at a time with per-key jitter."""
        await page.click(selector)
        for ch in text:
            delay = self._randint(self._b.keystroke_delay_ms.min, self._b.keystroke_delay_ms.max)
            await page.keyboard.type(ch, delay=delay)

    async def dwell(
        self,
        page: Page,
        dwell_ms: IntRange,
        mouse_events: IntRange,
        scroll: ScrollPolicy,
        *,
        area: dict | None = None,
        scroll_height: int | None = None,
        hover_frame: Frame | None = None,
        hover_offset: tuple[int, int] = (0, 0),
    ) -> int:
        """Stay on the page for a random duration within ``dwell_ms`` while
        emitting ``mouse_events`` interactions shaped by ``scroll``.

        ``area`` constrains cursor movement and starting position to a
        rectangle (e.g. an iframe's bounding box). When set, wheel events
        routed by Chromium land on the intended scroll container — needed
        on Naver blog posts, whose content is inside ``iframe#mainFrame``.
        When omitted, the whole viewport is used.

        ``scroll_height`` is the pixel-height of the scrollable content.
        The per-nudge step is derived from this and ``scroll.depth_ratio``
        so the reader lands at the targeted reading depth by the end.

        ``hover_frame`` is the frame to query for hoverable elements. For
        iframe-based pages, pass the content iframe's Frame so the element
        bounding boxes come from inside it. ``hover_offset`` is the
        (x, y) offset of that frame on the outer page, used to translate
        inner-frame coordinates back to outer-page coordinates for the
        ``page.mouse.move`` call.

        Returns the actual dwell duration in ms.
        """
        if area is None:
            vp = page.viewport_size or {"width": 1280, "height": 800}
            area = {"x": 0, "y": 0, "width": vp["width"], "height": vp["height"]}

        if scroll_height is None:
            try:
                scroll_height = int(await page.evaluate("document.body.scrollHeight"))
            except Exception:
                scroll_height = 1000
        if not scroll_height or scroll_height < 100:
            scroll_height = 1000

        # Anchor cursor inside the active area so the first wheel event
        # routes into it (Chromium dispatches wheel at the current position).
        cx = area["x"] + area["width"] / 2
        cy = area["y"] + area["height"] / 2
        await page.mouse.move(cx, cy)

        dwell_total_ms = self._randint(dwell_ms.min, dwell_ms.max)
        events = self._randint(mouse_events.min, mouse_events.max)
        per_event_s = (dwell_total_ms / 1000) / max(events, 1)

        depth = self._rng.uniform(scroll.depth_ratio.min, scroll.depth_ratio.max)
        target_y = depth * scroll_height
        n_scrolls = max(1, int(round(events * _SCROLL_EVENT_FRACTION)))
        n_moves = max(0, events - n_scrolls)
        net_factor = max(0.1, 2 * scroll.down_bias - 1)
        base_step = target_y / (n_scrolls * net_factor)

        plan = ["scroll"] * n_scrolls + ["move"] * n_moves
        self._rng.shuffle(plan)

        x_lo = int(area["x"]) + 50
        x_hi = max(x_lo, int(area["x"] + area["width"]) - 50)
        y_lo = int(area["y"]) + 50
        y_hi = max(y_lo, int(area["y"] + area["height"]) - 50)

        for action in plan:
            if action == "scroll":
                sign = 1 if self._rng.random() < scroll.down_bias else -1
                dy = int(base_step * self._rng.uniform(0.7, 1.3)) * sign
                await page.mouse.wheel(0, dy)
            else:
                # Non-scroll: some events hover a real element (fires
                # mouseover/mouseenter), others are plain random moves.
                point: tuple[int, int] | None = None
                if self._rng.random() < _HOVER_EVENT_FRACTION:
                    point = await self._pick_hover_point(
                        page, area, hover_frame, hover_offset,
                    )
                if point is None:
                    point = (self._randint(x_lo, x_hi), self._randint(y_lo, y_hi))
                await page.mouse.move(point[0], point[1], steps=self._randint(5, 20))
            await asyncio.sleep(per_event_s * self._rng.uniform(0.6, 1.4))
        return dwell_total_ms

    async def _pick_hover_point(
        self,
        page: Page,
        area: dict,
        hover_frame: Frame | None,
        hover_offset: tuple[int, int],
    ) -> tuple[int, int] | None:
        """Pick one visible element inside the active area and return the
        outer-page (x, y) to move the cursor to.

        ``hover_frame`` chooses which document to query. For iframe pages,
        pass the inner frame; the returned coordinates already account for
        the iframe's offset on the outer page.
        """
        target = hover_frame if hover_frame is not None else page.main_frame
        boxes = await query_element_centers(target, _HOVER_SELECTOR, min_dim=20)
        if not boxes:
            return None

        ox, oy = hover_offset
        ax0, ay0 = area["x"], area["y"]
        ax1, ay1 = ax0 + area["width"], ay0 + area["height"]

        viable: list[tuple[int, int]] = []
        for box in boxes:
            px = box["x"] + ox
            py = box["y"] + oy
            if ax0 <= px <= ax1 and ay0 <= py <= ay1:
                viable.append((int(px), int(py)))

        if not viable:
            return None
        return self._rng.choice(viable)

    async def dwell_with_activity(self, page: Page) -> int:
        """Dwell using the default Behavior block's ranges (search-page use).

        Uses a neutral scroll policy since the search-results page isn't
        being "read" — the only goal is to leave behavior-layer signals.
        """
        return await self.dwell(
            page, self._b.dwell_ms, self._b.mouse_events, _NEUTRAL_SCROLL,
        )

    async def inter_search_pause(self) -> None:
        """Sleep a randomized gap between consecutive searches, so that a
        single identity never emits a 'burst' the Firewall would flag.
        """
        seconds = self._randint(self._b.inter_search_s.min, self._b.inter_search_s.max)
        await asyncio.sleep(seconds)
