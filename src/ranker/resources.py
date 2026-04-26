"""Network-level resource blocking for proxy-traffic reduction.

Plays the role of an in-browser adblocker: at the BrowserContext layer,
every request goes through a route handler that aborts the ones the
manifest opted to drop. Requests that abort here never leave Chromium,
so they're free from a bandwidth (= proxy traffic) standpoint.

The 3rd-party-tracker list is intentionally conservative: only well-known
ad/analytics hosts. Anything embedded by Naver itself (``*.naver.com``,
``*.naver.net``, ``*.pstatic.net``) is always allowed so the session still
emits the 1st-party telemetry a real user would.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

from playwright.async_api import BrowserContext, Request, Route

from .manifest import Resources


# Substring-matched against the request hostname. Kept short and stable —
# we'd rather pass an unknown 3rd-party request than risk a false-positive
# on something Naver actually depends on.
_TRACKER_HOSTS: frozenset[str] = frozenset({
    "googletagmanager.com",
    "google-analytics.com",
    "doubleclick.net",
    "facebook.net",
    "scorecardresearch.com",
    "criteo.com",
    "criteo.net",
    "adsrvr.org",
    "adnxs.com",
    "moatads.com",
    "rubiconproject.com",
    "taboola.com",
    "outbrain.com",
})


def is_tracker_host(url: str) -> bool:
    """True if ``url``'s hostname matches a known 3rd-party tracker.

    Substring match — picks up subdomains (``analytics.google.com``,
    ``connect.facebook.net``, etc.) without an explicit list per variant.
    """
    host = urlparse(url).hostname or ""
    return any(t in host for t in _TRACKER_HOSTS)


@dataclass
class BlockCounter:
    """Per-Job tally — read at end-of-Job for the savings log line."""
    allowed: int = 0
    blocked: int = 0

    @property
    def total(self) -> int:
        return self.allowed + self.blocked

    def summary(self) -> str:
        if self.total == 0:
            return ""
        pct = self.blocked * 100 / self.total
        return f"blocked {self.blocked} of {self.total} requests ({pct:.0f}%)"


def make_route_handler(resources: Resources, counter: BlockCounter):
    """Build a route handler bound to one Resources policy + counter.

    The closure captures the policy as a frozenset for fast membership
    checks per request — the handler is on the request hot-path, so we
    avoid re-deriving it on every call.
    """
    blocked_types: frozenset[str] = frozenset(rt.value for rt in resources.block)
    block_trackers = resources.block_third_party_trackers

    async def handler(route: Route, request: Request) -> None:
        if request.resource_type in blocked_types:
            counter.blocked += 1
            await route.abort()
            return
        if block_trackers and is_tracker_host(request.url):
            counter.blocked += 1
            await route.abort()
            return
        counter.allowed += 1
        await route.continue_()

    return handler


async def install_blocking(
    context: BrowserContext, resources: Resources, counter: BlockCounter,
) -> None:
    """Register the blocking route on ``context``. ``counter`` is shared
    across rotations within the same Job so end-of-Job totals reflect the
    full Job, not just the most recent context."""
    handler = make_route_handler(resources, counter)
    await context.route("**/*", handler)
