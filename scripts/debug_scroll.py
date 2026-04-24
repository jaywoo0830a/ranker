"""Diagnose why scrolling a Naver blog post doesn't take effect.

Usage:
    uv run python scripts/debug_scroll.py <blog-post-url>

Opens the page in a headful Chromium, analyses the iframe structure, tries
three different scroll techniques, and reports what actually changed. Leaves
the browser open afterward so you can inspect manually; press Enter in the
terminal to close.

Typical output reveals one of:
  - No iframe at all (non-iframe blog theme → outer document scrolls)
  - iframe present, same-origin, but scroll container is a nested element
    (e.g. a div with ``overflow: auto`` inside the iframe body)
  - iframe present but cross-origin (contentDocument throws)
  - iframe's documentElement scrolls instead of body (HTML5 mode)
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from playwright.async_api import Frame, Page, async_playwright


_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _dump(label: str, value: Any) -> None:
    print(f"\n=== {label} ===")
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


async def _outer_inspection(page: Page) -> dict:
    return await page.evaluate(
        """() => {
            const iframes = Array.from(document.querySelectorAll('iframe'));
            return {
                outer_body_scrollHeight: document.body.scrollHeight,
                outer_body_clientHeight: document.body.clientHeight,
                outer_documentElement_scrollHeight: document.documentElement.scrollHeight,
                iframes: iframes.map(f => ({
                    id: f.id,
                    name: f.name,
                    src: f.src.slice(0, 120),
                    offsetWidth: f.offsetWidth,
                    offsetHeight: f.offsetHeight,
                    rect: (() => {
                        const r = f.getBoundingClientRect();
                        return {x: r.x, y: r.y, w: r.width, h: r.height};
                    })(),
                })),
            };
        }"""
    )


async def _contentdoc_probe(page: Page) -> dict:
    """Try same-origin access through the outer page's JS."""
    return await page.evaluate(
        """() => {
            const iframe = document.querySelector('iframe#mainFrame')
                        || document.querySelector('iframe');
            if (!iframe) return {error: 'no iframe element'};
            try {
                const doc = iframe.contentDocument;
                if (!doc) return {error: 'contentDocument is null (cross-origin?)'};
                const win = iframe.contentWindow;
                return {
                    ok: true,
                    inner_body_scrollHeight: doc.body?.scrollHeight ?? null,
                    inner_body_clientHeight: doc.body?.clientHeight ?? null,
                    inner_documentElement_scrollHeight: doc.documentElement?.scrollHeight ?? null,
                    inner_documentElement_clientHeight: doc.documentElement?.clientHeight ?? null,
                    inner_window_scrollY: win?.scrollY ?? null,
                    inner_window_innerHeight: win?.innerHeight ?? null,
                };
            } catch (e) {
                return {error: 'exception', message: String(e)};
            }
        }"""
    )


async def _find_main_frame(page: Page) -> Frame | None:
    """Pick the frame most likely to host the post content."""
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        if frame.name == "mainFrame":
            return frame
        if "PostView" in frame.url or "blog.naver.com" in frame.url:
            return frame
    # Fallback: first non-main, if any
    for frame in page.frames:
        if frame != page.main_frame:
            return frame
    return None


async def _find_scroll_container(frame: Frame) -> dict:
    """Inside the iframe, find the element that actually scrolls."""
    return await frame.evaluate(
        """() => {
            const scrollables = [];
            const all = document.querySelectorAll('*');
            for (const el of all) {
                const s = window.getComputedStyle(el);
                const overflowY = s.overflowY;
                const canScroll = (overflowY === 'scroll' || overflowY === 'auto');
                const hasExcess = el.scrollHeight > el.clientHeight + 1;
                if (canScroll && hasExcess) {
                    scrollables.push({
                        tag: el.tagName,
                        id: el.id,
                        class: (el.className && el.className.substring)
                            ? el.className.substring(0, 80) : '',
                        scrollHeight: el.scrollHeight,
                        clientHeight: el.clientHeight,
                    });
                }
            }
            return {
                window_scrollY: window.scrollY,
                body_scrollHeight: document.body.scrollHeight,
                body_clientHeight: document.body.clientHeight,
                documentElement_scrollHeight: document.documentElement.scrollHeight,
                documentElement_clientHeight: document.documentElement.clientHeight,
                overflow_scrollable_elements: scrollables.slice(0, 5),
            };
        }"""
    )


async def _read_scroll_state(page: Page, frame: Frame | None) -> dict:
    state: dict[str, Any] = {}
    state["outer_window_scrollY"] = await page.evaluate("window.scrollY")
    if frame is not None:
        state["inner_window_scrollY"] = await frame.evaluate("window.scrollY")
        state["inner_body_scrollTop"] = await frame.evaluate(
            "document.body ? document.body.scrollTop : null"
        )
        state["inner_documentElement_scrollTop"] = await frame.evaluate(
            "document.documentElement.scrollTop"
        )
    return state


async def _reset_scroll(page: Page, frame: Frame | None) -> None:
    await page.evaluate("window.scrollTo(0, 0)")
    if frame is not None:
        await frame.evaluate(
            "() => { window.scrollTo(0, 0);"
            " if (document.body) document.body.scrollTop = 0;"
            " document.documentElement.scrollTop = 0; }"
        )
    await asyncio.sleep(0.3)


async def diagnose(url: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent=_UA,
            locale="ko-KR",
            timezone_id="Asia/Seoul",
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()
        print(f"→ Navigating to {url}")
        await page.goto(url, wait_until="domcontentloaded")
        # Give the iframe's contentDocument time to populate.
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass
        await asyncio.sleep(1.5)

        _dump("Frame tree (Playwright)", [
            {"name": f.name, "url": f.url[:120], "is_main": f == page.main_frame}
            for f in page.frames
        ])
        _dump("Outer document", await _outer_inspection(page))
        _dump("contentDocument probe (outer JS)", await _contentdoc_probe(page))

        main_frame = await _find_main_frame(page)
        if main_frame is None:
            print("\n⚠ No candidate content iframe found.")
        else:
            print(f"\n→ Using frame: name={main_frame.name!r} url={main_frame.url[:120]}")
            _dump("Inner frame structure", await _find_scroll_container(main_frame))

        # Scroll tests
        print("\n=== Scroll method tests (+500 px each) ===")
        await _reset_scroll(page, main_frame)
        baseline = await _read_scroll_state(page, main_frame)
        _dump("baseline", baseline)

        # A: outer page wheel centred on the iframe
        outer = await _outer_inspection(page)
        if outer["iframes"]:
            rect = outer["iframes"][0]["rect"]
            cx = rect["x"] + rect["w"] / 2
            cy = rect["y"] + rect["h"] / 2
        else:
            cx, cy = 640, 400
        await page.mouse.move(cx, cy)
        await page.mouse.wheel(0, 500)
        await asyncio.sleep(1.0)
        _dump(f"A) page.mouse.wheel at ({cx:.0f}, {cy:.0f})",
              await _read_scroll_state(page, main_frame))

        # B: frame.evaluate window.scrollBy
        await _reset_scroll(page, main_frame)
        if main_frame is not None:
            await main_frame.evaluate("window.scrollBy(0, 500)")
            await asyncio.sleep(0.8)
            _dump("B) frame.evaluate window.scrollBy(0, 500)",
                  await _read_scroll_state(page, main_frame))

        # C: outer-JS reaching into iframe.contentWindow
        await _reset_scroll(page, main_frame)
        try:
            await page.evaluate(
                """() => {
                    const iframe = document.querySelector('iframe#mainFrame')
                                || document.querySelector('iframe');
                    if (iframe && iframe.contentWindow) {
                        iframe.contentWindow.scrollBy(0, 500);
                    }
                }"""
            )
            await asyncio.sleep(0.8)
            _dump("C) iframe.contentWindow.scrollBy(0, 500) from outer JS",
                  await _read_scroll_state(page, main_frame))
        except Exception as e:
            print(f"C failed: {e}")

        # D: if there's a nested scroll container inside the iframe, try it
        if main_frame is not None:
            await _reset_scroll(page, main_frame)
            nested = await main_frame.evaluate(
                """() => {
                    const candidates = [];
                    for (const el of document.querySelectorAll('*')) {
                        const s = window.getComputedStyle(el);
                        if ((s.overflowY === 'scroll' || s.overflowY === 'auto') &&
                            el.scrollHeight > el.clientHeight + 1) {
                            candidates.push(el);
                        }
                    }
                    if (candidates.length === 0) return null;
                    const el = candidates[0];
                    el.scrollTop = 500;
                    return {
                        tag: el.tagName,
                        id: el.id,
                        class: (el.className && el.className.substring)
                            ? el.className.substring(0, 80) : '',
                        scrollTop_after: el.scrollTop,
                    };
                }"""
            )
            _dump("D) Nested overflow container .scrollTop = 500", nested)

        print("\n=== Diagnosis complete. Browser stays open — press Enter to close. ===")
        try:
            await asyncio.to_thread(input, "")
        except (EOFError, KeyboardInterrupt):
            pass
        await browser.close()


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] in ("-h", "--help"):
        print("usage: debug_scroll.py <url>", file=sys.stderr)
        return 2
    asyncio.run(diagnose(sys.argv[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
