"""Mode profiles — presets that bundle everything a mode implies.

``mode: desktop`` and ``mode: mobile`` differ across the BrowserContext
identity (UA, viewport, touch flags) and the integrated-search URL.
Collecting them into one ``ModeProfile`` keeps mode-switching in a single
place — callers look up the active profile rather than branching on
``mode`` everywhere.

The mobile profile's selectors are **best-guess placeholders**; confirm
with ``scripts/probe_mobile.py`` on a live mobile search and adjust
``MOBILE.default_sections`` if needed.
"""

from __future__ import annotations

from dataclasses import dataclass

from .manifest import Locator, LocatorType, Mode, SectionLocators, Viewport


@dataclass(frozen=True)
class ModeProfile:
    """Everything that varies between desktop and mobile operation.

    ``search_url_template`` is the URL (with ``{query}`` placeholder) used
    to reach integrated search results directly. Skipping the
    homepage→type→submit dance avoids Naver's fragile mobile overlay JS
    and the CAPTCHA surfaces that typed searches can trigger.

    ``default_sections`` is the fallback used when the manifest omits
    ``source.sections`` — letting users write a 3-line mobile manifest.
    """
    mode: Mode
    user_agents: tuple[str, ...]
    viewport: Viewport
    is_mobile: bool
    has_touch: bool
    device_scale_factor: float
    search_url_template: str
    default_sections: SectionLocators


def _css(value: str) -> Locator:
    return Locator(type=LocatorType.CSS, value=value)


DESKTOP = ModeProfile(
    mode=Mode.DESKTOP,
    user_agents=(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    ),
    viewport=Viewport(width=1280, height=800),
    is_mobile=False,
    has_touch=False,
    device_scale_factor=1.0,
    search_url_template=(
        "https://search.naver.com/search.naver"
        "?where=nexearch&sm=top_hty&fbm=0&ie=utf8&query={query}"
    ),
    default_sections=SectionLocators(
        head=_css("div.spw_rerank._slog_visible._rra_head"),
        body=_css("div.spw_rerank._slog_visible._rra_body"),
    ),
)


MOBILE = ModeProfile(
    mode=Mode.MOBILE,
    user_agents=(
        # iPhone Safari — common mobile Naver audience.
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 "
        "Mobile/15E148 Safari/604.1",
        # Android Chrome.
        "Mozilla/5.0 (Linux; Android 14; SM-G991N) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 "
        "Mobile Safari/537.36",
    ),
    # iPhone 14-ish — a neutral modern-phone size.
    viewport=Viewport(width=414, height=896),
    is_mobile=True,
    has_touch=True,
    device_scale_factor=2.0,
    search_url_template=(
        "https://m.search.naver.com/search.naver"
        "?where=m&sm=mtp_hty.top&query={query}"
    ),
    # PLACEHOLDER — mobile Naver may serve different class names. Run
    # scripts/probe_mobile.py to confirm; update if they don't match.
    default_sections=SectionLocators(
        head=_css("div.spw_rerank._slog_visible._rra_head"),
        body=_css("div.spw_rerank._slog_visible._rra_body"),
    ),
)


_PROFILES: dict[Mode, ModeProfile] = {
    Mode.DESKTOP: DESKTOP,
    Mode.MOBILE: MOBILE,
}


def profile_for(mode: Mode) -> ModeProfile:
    """Return the preset bundle for the active mode."""
    return _PROFILES[mode]
