"""BrowserContext lifecycle + rotation policy.

Mirrors the reference Firewall's identity model: a Playwright BrowserContext
is roughly one ``session_id`` from the defender's point of view. Rotating it
between runs prevents the session from piling up signals that would look
like a burst to any aggregate-layer detector.

The per-mode identity surface (UA set, viewport, mobile/touch flags) comes
from :mod:`ranker.profile`; this module just owns the rotation bookkeeping.

When a ``Proxy`` is configured, every rotation also mints a fresh sticky
session ID (SID) and rebuilds the proxy username — so a new BrowserContext
gets a new upstream IP from the provider's pool.
"""

from __future__ import annotations

from playwright.async_api import Browser, BrowserContext

from .manifest import Identity, Proxy, RotatePolicy
from .profile import ModeProfile
from .proxy import build_proxy_arg, new_session_id


class ContextPool:
    """Hands out BrowserContexts according to the rotation policy.

    ``per_run``:    rotate before each full run of all targets.
    ``per_target``: rotate before every single target (strongest isolation).
    ``never``:      one context for the pool's lifetime.

    If ``proxy`` is provided, ``account_stem`` and ``password`` (loaded
    once from env at startup) are reused across rotations while the SID is
    minted anew each time — that's how IP isolation per rotation works.
    """

    def __init__(
        self,
        browser: Browser,
        identity: Identity,
        profile: ModeProfile,
        proxy: Proxy | None = None,
        account_stem: str | None = None,
        password: str | None = None,
    ) -> None:
        if proxy is not None and (account_stem is None or password is None):
            raise ValueError(
                "ContextPool: proxy is set but credentials are missing — "
                "load them from env via proxy.require_credentials() at startup."
            )
        self._browser = browser
        self._identity = identity
        self._profile = profile
        self._proxy = proxy
        self._account_stem = account_stem
        self._password = password
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

    async def force_rotate(self) -> BrowserContext:
        """Rotate immediately, regardless of policy. Used to recover from
        transient proxy/tunnel failures by minting a fresh sticky session
        (and therefore a fresh upstream IP). The returned context replaces
        the previous one — any pages on the old context are dead."""
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
        ua = self._profile.user_agents[self._rotation_count % len(self._profile.user_agents)]
        viewport = self._identity.viewport or self._profile.viewport
        proxy_kwargs = {}
        if self._proxy is not None:
            assert self._account_stem is not None and self._password is not None
            arg = build_proxy_arg(
                self._proxy, self._account_stem, self._password, new_session_id(),
            )
            proxy_kwargs["proxy"] = arg.to_playwright()
        self._current = await self._browser.new_context(
            user_agent=ua,
            locale=self._identity.locale,
            timezone_id=self._identity.timezone,
            viewport={"width": viewport.width, "height": viewport.height},
            is_mobile=self._profile.is_mobile,
            has_touch=self._profile.has_touch,
            device_scale_factor=self._profile.device_scale_factor,
            **proxy_kwargs,
        )
        # Strip the tell-tale ``navigator.webdriver`` flag before any page
        # script runs in the context.
        await self._current.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        self._rotation_count += 1
