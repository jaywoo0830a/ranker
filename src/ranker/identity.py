"""BrowserContext lifecycle + rotation policy.

Mirrors the reference Firewall's identity model: a Playwright BrowserContext
is roughly one ``session_id`` from the defender's point of view. Rotating it
between runs prevents the session from piling up signals that would look
like a burst to any aggregate-layer detector.
"""

from __future__ import annotations

from playwright.async_api import Browser, BrowserContext

from .manifest import Identity, RotatePolicy


_REAL_USER_AGENTS: tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
)


class ContextPool:
    """Hands out BrowserContexts according to the rotation policy.

    ``per_run``:    rotate before each full run of all targets.
    ``per_target``: rotate before every single target (strongest isolation).
    ``never``:      one context for the pool's lifetime.
    """

    def __init__(self, browser: Browser, identity: Identity) -> None:
        self._browser = browser
        self._identity = identity
        self._current: BrowserContext | None = None
        self._rotation_count = 0

    async def begin_run(self) -> BrowserContext:
        if self._identity.rotate_context == RotatePolicy.PER_RUN or self._current is None:
            await self._rotate()
        assert self._current is not None
        return self._current

    async def begin_target(self) -> BrowserContext:
        if self._identity.rotate_context == RotatePolicy.PER_TARGET or self._current is None:
            await self._rotate()
        assert self._current is not None
        return self._current

    async def close(self) -> None:
        if self._current is not None:
            await self._current.close()
            self._current = None

    async def _rotate(self) -> None:
        if self._current is not None:
            await self._current.close()
        ua = _REAL_USER_AGENTS[self._rotation_count % len(_REAL_USER_AGENTS)]
        self._current = await self._browser.new_context(
            user_agent=ua,
            locale=self._identity.locale,
            timezone_id=self._identity.timezone,
            viewport={
                "width": self._identity.viewport.width,
                "height": self._identity.viewport.height,
            },
        )
        # Strip the tell-tale ``navigator.webdriver`` flag before any page
        # script runs in the context.
        await self._current.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        self._rotation_count += 1
