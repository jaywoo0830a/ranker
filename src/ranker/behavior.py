"""Human-like interaction helpers layered on top of Playwright.

These helpers encode the defense principles from the reference material:
behavior signals (dwell, mouse events) must be present and plausible, and
timing must vary. All randomness flows through an injected ``random.Random``
so tests can pin a seed.
"""

from __future__ import annotations

import asyncio
import random

from playwright.async_api import Page

from .manifest import Behavior


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

    async def dwell_with_activity(self, page: Page) -> None:
        """Stay on the page for a plausible duration while generating mouse
        events — matching the reference Firewall's behavior-layer expectations
        (no long dwell with zero interaction).
        """
        dwell_total = self._randint(self._b.dwell_ms.min, self._b.dwell_ms.max) / 1000
        events = self._randint(self._b.mouse_events.min, self._b.mouse_events.max)
        per_event = dwell_total / max(events, 1)

        viewport = page.viewport_size or {"width": 1280, "height": 800}
        for _ in range(events):
            action = self._rng.choice(("wheel", "move"))
            if action == "wheel":
                dy = self._randint(200, 700) * self._rng.choice((-1, 1))
                await page.mouse.wheel(0, dy)
            else:
                x = self._randint(50, viewport["width"] - 50)
                y = self._randint(50, viewport["height"] - 50)
                await page.mouse.move(x, y, steps=self._randint(5, 20))
            await asyncio.sleep(per_event * self._rng.uniform(0.6, 1.4))

    async def inter_search_pause(self) -> None:
        """Sleep a randomized gap between consecutive searches, so that a
        single identity never emits a 'burst' the Firewall would flag.
        """
        seconds = self._randint(self._b.inter_search_s.min, self._b.inter_search_s.max)
        await asyncio.sleep(seconds)
