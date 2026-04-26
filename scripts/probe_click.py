"""Capture whatever element you actually tap on a mobile-emulated page.

Usage:
    uv run python scripts/probe_click.py [url]
    (default url: https://m.naver.com/)

Opens the URL in headful mobile emulation, installs a click listener, and
waits for you to tap the real search-bar trigger in the browser window.
Prints the full DOM path + resulting URL of every click so you can pick
the right selector for ``MOBILE.search_trigger`` in ``profile.py``.

Press Enter in the terminal when done to close.
"""

from __future__ import annotations

import asyncio
import sys

from playwright.async_api import async_playwright


_IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 "
    "Mobile/15E148 Safari/604.1"
)

_CLICK_LOGGER = r"""
(() => {
  window.__clicks = [];
  const handler = (e) => {
    const path = [];
    let el = e.target;
    while (el && el !== document.documentElement) {
      let s = el.tagName ? el.tagName.toLowerCase() : String(el);
      if (el.id) s += '#' + el.id;
      const cls = el.className;
      if (typeof cls === 'string' && cls.trim()) {
        const c = cls.trim().split(/\s+/).filter(Boolean);
        if (c.length) s += '.' + c.join('.');
      }
      path.unshift(s);
      el = el.parentElement;
    }
    let cx = null, cy = null;
    if (e.clientX != null) { cx = e.clientX; cy = e.clientY; }
    else if (e.changedTouches && e.changedTouches.length) {
      cx = e.changedTouches[0].clientX;
      cy = e.changedTouches[0].clientY;
    }
    window.__clicks.push({
      event: e.type,
      path: path.join(' > '),
      tag: e.target.tagName,
      role: e.target.getAttribute && e.target.getAttribute('role'),
      href_closest: (e.target.closest && e.target.closest('a'))
        ? e.target.closest('a').href : null,
      x: cx, y: cy,
      url_when_clicked: location.href,
      time: Date.now(),
    });
  };
  document.addEventListener('click', handler, true);
  document.addEventListener('touchend', handler, true);
})();
"""


async def probe(url: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent=_IPHONE_UA,
            locale="ko-KR",
            timezone_id="Asia/Seoul",
            viewport={"width": 414, "height": 896},
            is_mobile=True,
            has_touch=True,
            device_scale_factor=2,
        )
        # Install logger in every page/frame the context creates, so it
        # survives SPA transitions that might replace the document.
        await context.add_init_script(_CLICK_LOGGER)

        page = await context.new_page()
        print(f"→ Navigating to {url}")
        await page.goto(url, wait_until="domcontentloaded")
        try:
            await page.wait_for_load_state("networkidle", timeout=8_000)
        except Exception:
            pass

        print("\n→ Browser is open. In the window, TAP whatever acts as the")
        print("  search-bar trigger (the fake search bar on m.naver.com).")
        print("  Clicks get printed here as they happen. Press Enter when done.\n")

        # Poll for new click records while waiting for user input.
        async def poller() -> None:
            seen = 0
            while True:
                try:
                    clicks = await page.evaluate("window.__clicks || []")
                except Exception:
                    # Page may be navigating; retry.
                    await asyncio.sleep(0.4)
                    continue
                for rec in clicks[seen:]:
                    print(f"  event={rec['event']} tag={rec['tag']} role={rec['role']}")
                    print(f"  path: {rec['path']}")
                    if rec.get("x") is not None:
                        print(f"  coords: ({rec['x']}, {rec['y']})")
                    if rec.get("href_closest"):
                        print(f"  → closest <a href>: {rec['href_closest']}")
                    print(f"  url at click: {rec['url_when_clicked']}")
                    print(f"  url now:      {page.url}")
                    print()
                seen = len(clicks)
                await asyncio.sleep(0.4)

        poll_task = asyncio.create_task(poller())
        try:
            await asyncio.to_thread(input, "")
        except (EOFError, KeyboardInterrupt):
            pass
        poll_task.cancel()

        await browser.close()


def main() -> int:
    if len(sys.argv) > 2 or (len(sys.argv) == 2 and sys.argv[1] in ("-h", "--help")):
        print("usage: probe_click.py [url]", file=sys.stderr)
        return 2
    url = sys.argv[1] if len(sys.argv) == 2 else "https://m.naver.com/"
    asyncio.run(probe(url))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
