"""Discover the real selectors Naver mobile serves for integrated search.

Usage:
    uv run python scripts/probe_mobile.py "<검색 키워드>"

Opens ``m.naver.com`` in a headful mobile emulation, types the keyword into
the search box, and on the results page dumps candidate result-block
containers — matching patterns the desktop site uses (``spw_rerank``,
``_rra_*``), plus anything that looks like a repeated-list container.

Output goes to stdout so you can eyeball the class names; plug the winning
CSS selectors into ``src/ranker/profile.py`` under ``MOBILE.default_sections``
(or into your manifest's ``source.sections`` as an override).

Leaves the browser open afterward for manual inspection; press Enter to close.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from playwright.async_api import async_playwright


_IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 "
    "Mobile/15E148 Safari/604.1"
)


def _dump(label: str, value: Any) -> None:
    print(f"\n=== {label} ===")
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


async def probe(keyword: str) -> None:
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
        page = await context.new_page()
        print("→ Navigating to https://m.naver.com/")
        await page.goto("https://m.naver.com/", wait_until="domcontentloaded")
        try:
            await page.wait_for_load_state("networkidle", timeout=8_000)
        except Exception:
            pass

        # Try to find the search input.
        search_input_info = await page.evaluate(
            """() => {
                const candidates = [
                    'input#query',
                    'input[name="query"]',
                    'input.search_input',
                    'input[type="search"]',
                ];
                for (const sel of candidates) {
                    const el = document.querySelector(sel);
                    if (el) {
                        return {
                            matched: sel,
                            id: el.id, name: el.name, class: el.className,
                            placeholder: el.placeholder,
                        };
                    }
                }
                return {matched: null};
            }"""
        )
        _dump("search input probe", search_input_info)

        input_selector = search_input_info.get("matched") or "input#query"
        print(f"→ Typing keyword {keyword!r} into {input_selector}")
        await page.click(input_selector)
        await page.keyboard.type(keyword, delay=90)
        await page.keyboard.press("Enter")

        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass
        await asyncio.sleep(1.5)
        print(f"→ Results page: {page.url}")

        # Report top-level containers whose classes look relevant.
        summary = await page.evaluate(
            """() => {
                const out = {
                    has_spw_rerank: document.querySelectorAll(
                        'div.spw_rerank._slog_visible'
                    ).length,
                    has_rra_head: document.querySelectorAll(
                        'div.spw_rerank._slog_visible._rra_head'
                    ).length,
                    has_rra_body: document.querySelectorAll(
                        'div.spw_rerank._slog_visible._rra_body'
                    ).length,
                };
                // Group elements by class signature; report repeated containers.
                const classBuckets = {};
                for (const el of document.querySelectorAll('section, div, ul')) {
                    const cls = (el.className && el.className.split)
                        ? el.className.split(/\\s+/).filter(Boolean).sort().join(' ')
                        : '';
                    if (!cls) continue;
                    if (!classBuckets[cls]) classBuckets[cls] = 0;
                    classBuckets[cls]++;
                }
                // Show class signatures that appear 2+ times (result lists).
                const repeated = Object.entries(classBuckets)
                    .filter(([_, n]) => n >= 2 && n <= 50)
                    .map(([cls, n]) => ({classes: cls, count: n}))
                    .slice(0, 40);
                return {
                    signals: out,
                    repeated_class_signatures_sample: repeated,
                };
            }"""
        )
        _dump("integrated-search signals", summary["signals"])
        _dump("repeated class signatures (candidate list containers)",
              summary["repeated_class_signatures_sample"])

        # If the desktop-style containers exist, dump their anchor list.
        anchors = await page.evaluate(
            """() => {
                const out = {};
                for (const key of ['_rra_head', '_rra_body']) {
                    const node = document.querySelector(
                        `div.spw_rerank._slog_visible.${key}`
                    );
                    if (!node) {
                        out[key] = null;
                        continue;
                    }
                    const list = [];
                    for (const a of node.querySelectorAll('a[href]')) {
                        const t = (a.innerText || a.textContent || '').trim();
                        list.push({url: a.href, title: t.slice(0, 80)});
                    }
                    out[key] = list.slice(0, 15);
                }
                return out;
            }"""
        )
        _dump("anchors inside spw_rerank blocks (if any)", anchors)

        print("\n=== Probe complete. Browser stays open — press Enter to close. ===")
        try:
            await asyncio.to_thread(input, "")
        except (EOFError, KeyboardInterrupt):
            pass
        await browser.close()


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] in ("-h", "--help"):
        print("usage: probe_mobile.py <keyword>", file=sys.stderr)
        return 2
    asyncio.run(probe(sys.argv[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
