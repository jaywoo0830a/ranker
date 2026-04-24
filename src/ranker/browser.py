"""Generic Playwright helpers shared across the ranker modules.

These functions are page-type agnostic — they work on any page regardless
of origin, layout, or domain. Domain-specific helpers (iframe detection
for Naver blog, result-block extraction, blog_id matching, etc.) stay in
``search.py``.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, TypeVar

from playwright.async_api import Frame, Page, TimeoutError as PWTimeoutError


T = TypeVar("T")


async def wait_stable(
    page: Page,
    *,
    ready_selector: str | None = None,
    ready_timeout_ms: int = 10_000,
    network_timeout_ms: int = 8_000,
    settle_ms: int = 400,
) -> None:
    """Wait for a page to reach a rendered, network-idle state.

    All three steps are best-effort — on timeout we move on rather than
    raise, because the caller usually has its own follow-up check (e.g.
    waiting for a specific element) that's the real gate for progress.

      1. ``networkidle`` — no network activity for 500ms.
      2. ``ready_selector`` visible (optional) — the page-specific element
         that proves the critical content has mounted.
      3. Small settle sleep for any post-load client JS.
    """
    try:
        await page.wait_for_load_state("networkidle", timeout=network_timeout_ms)
    except PWTimeoutError:
        pass
    if ready_selector is not None:
        try:
            await page.wait_for_selector(
                ready_selector, state="visible", timeout=ready_timeout_ms,
            )
        except PWTimeoutError:
            pass
    await asyncio.sleep(settle_ms / 1000)


async def goto_stable(
    page: Page,
    url: str,
    *,
    ready_selector: str | None = None,
    ready_timeout_ms: int = 10_000,
    network_timeout_ms: int = 8_000,
    settle_ms: int = 400,
) -> None:
    """``page.goto`` followed by :func:`wait_stable` — the canonical
    'navigate there and let it settle' pattern used everywhere."""
    await page.goto(url, wait_until="domcontentloaded")
    await wait_stable(
        page,
        ready_selector=ready_selector,
        ready_timeout_ms=ready_timeout_ms,
        network_timeout_ms=network_timeout_ms,
        settle_ms=settle_ms,
    )


async def wait_for_any(
    page: Page,
    selectors: tuple[str, ...] | list[str],
    *,
    timeout_ms: int,
) -> bool:
    """Return True once any of ``selectors`` appears; False on timeout.

    Useful when the page may render either one container or another (e.g.
    Naver's head or body result block) and we just need whichever.
    """
    try:
        await page.wait_for_selector(", ".join(selectors), timeout=timeout_ms)
        return True
    except PWTimeoutError:
        return False


async def poll_until(
    get_value: Callable[[], Awaitable[T | None]],
    *,
    predicate: Callable[[T | None], bool],
    timeout_ms: int,
    interval_ms: int = 300,
) -> T | None:
    """Poll ``get_value`` until ``predicate`` accepts the value.

    Returns the first value that passes, or the last value seen on timeout.
    Use for conditions Playwright's built-in waits can't express — e.g.
    'iframe inner scrollHeight exceeds N pixels'.
    """
    interval_s = interval_ms / 1000
    attempts = max(1, int(timeout_ms / max(interval_ms, 1)))
    value: T | None = None
    for _ in range(attempts):
        value = await get_value()
        if predicate(value):
            return value
        await asyncio.sleep(interval_s)
    return value


async def query_element_centers(
    target: Page | Frame,
    selector: str,
    *,
    min_dim: int = 20,
) -> list[dict[str, float]]:
    """Return the ``[{x, y}, …]`` centers of visible elements matching
    ``selector`` inside ``target`` (a Page or Frame).

    Coordinates are in ``target``'s own coordinate system — callers using
    an iframe frame must add the iframe's outer offset themselves.
    """
    try:
        return await target.evaluate(
            """(args) => {
                const {sel, minDim} = args;
                const out = [];
                for (const el of document.querySelectorAll(sel)) {
                    const r = el.getBoundingClientRect();
                    if (r.width >= minDim && r.height >= minDim) {
                        out.push({
                            x: r.left + r.width / 2,
                            y: r.top + r.height / 2,
                        });
                    }
                }
                return out;
            }""",
            {"sel": selector, "minDim": min_dim},
        )
    except Exception:
        return []


async def first_visible_bounding_box(
    page: Page,
    selector: str,
    *,
    timeout_ms: int = 3_000,
    min_dim: int = 0,
) -> dict | None:
    """Wait for ``selector`` to be visible and return its bounding box.

    Returns ``None`` on timeout or when the element is smaller than
    ``min_dim`` in either dimension (guards against tracking pixels /
    tiny placeholders).
    """
    try:
        handle = await page.wait_for_selector(
            selector, state="visible", timeout=timeout_ms,
        )
    except PWTimeoutError:
        return None
    if handle is None:
        return None
    box = await handle.bounding_box()
    if box is None:
        return None
    if box["width"] < min_dim or box["height"] < min_dim:
        return None
    return box
