"""Proxy session bookkeeping — username assembly + Playwright arg shaping.

ProxyEmpire encodes geo + session policy into the proxy *username* itself
(``<account>-country-<cc>-sid-<id>-ttl-<n>m``), so per-context rotation is
just "mint a new SID and rebuild the username". Credentials live in env
vars (``PROXYEMPIRE_USERNAME`` / ``PROXYEMPIRE_PASSWORD``), never in the
manifest.

This module only knows about the wire-level shape; ContextPool decides
*when* to mint a new SID (each rotation).
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass

from .manifest import Proxy, ProxyProvider


# Per-provider credential env vars. Hardcoded by provider rather than
# configured in the manifest — the manifest shouldn't need to know the
# names of secrets it doesn't carry.
_PROVIDER_ENV: dict[ProxyProvider, tuple[str, str]] = {
    ProxyProvider.PROXYEMPIRE: ("PROXYEMPIRE_USERNAME", "PROXYEMPIRE_PASSWORD"),
}


def env_var_names(provider: ProxyProvider) -> tuple[str, str]:
    """The (username, password) env var names for the given provider."""
    return _PROVIDER_ENV[provider]


def require_credentials(provider: ProxyProvider) -> tuple[str, str]:
    """Read credentials from env, raising a clear error if either is missing.

    Called at startup (not lazily on first rotation) so misconfigured
    deploys fail before any browser launches.
    """
    user_var, pass_var = env_var_names(provider)
    username = os.environ.get(user_var)
    password = os.environ.get(pass_var)
    missing = [name for name, val in ((user_var, username), (pass_var, password)) if not val]
    if missing:
        raise RuntimeError(
            f"proxy block requires env vars: {', '.join(missing)}. "
            f"Set them or remove the 'proxy:' block to run without a proxy."
        )
    assert username and password
    return username, password


def new_session_id() -> str:
    """Mint a fresh sticky-session ID. 8 hex chars matches ProxyEmpire's
    documented SID width and is plenty unique for a single account."""
    return secrets.token_hex(4)


@dataclass(frozen=True)
class ProxyArg:
    """Playwright-compatible proxy argument.

    Built once per rotation and handed to ``browser.new_context(proxy=...)``.
    Held as a dataclass (not a dict) so it's harder to accidentally print
    the password via ``repr``.
    """
    server: str
    username: str
    password: str

    def to_playwright(self) -> dict[str, str]:
        return {"server": self.server, "username": self.username, "password": self.password}

    def __repr__(self) -> str:
        # Mask the secret if anyone logs us. Username is also semi-sensitive
        # (it embeds the account stem) so we only show the SID portion.
        # Username shape: ``...-sid-<id>-ttl-...`` — slice the SID value
        # from between those markers.
        parts = self.username.split("-")
        sid_value = "<no-sid>"
        for i, seg in enumerate(parts[:-1]):
            if seg == "sid":
                sid_value = parts[i + 1]
                break
        return f"ProxyArg(server={self.server!r}, sid={sid_value}, password=***)"


def build_username(proxy: Proxy, account_stem: str, session_id: str) -> str:
    """Compose a ProxyEmpire username from its parts.

    Format: ``<stem>-country-<cc>-sid-<id>-ttl-<n>m``

    Other providers will need their own builder when added; today only
    ProxyEmpire is supported (and the schema enforces that), so we keep
    a single shape rather than a strategy table.
    """
    if proxy.provider != ProxyProvider.PROXYEMPIRE:  # pragma: no cover
        raise NotImplementedError(f"unsupported provider: {proxy.provider}")
    return (
        f"{account_stem}"
        f"-country-{proxy.country}"
        f"-sid-{session_id}"
        f"-ttl-{proxy.session_ttl_minutes}m"
    )


def build_proxy_arg(proxy: Proxy, account_stem: str, password: str, session_id: str) -> ProxyArg:
    """Assemble the full proxy argument for a single BrowserContext."""
    username = build_username(proxy, account_stem, session_id)
    server = f"http://{proxy.host}:{proxy.port}"
    return ProxyArg(server=server, username=username, password=password)


# Chromium net errors that mean "the tunnel itself didn't come up". Mobile
# carrier NAT pools occasionally drop the first CONNECT through a fresh
# sticky session before the route stabilizes — retrying with a new SID
# almost always lands on a healthy path.
_TRANSIENT_PROXY_MARKERS = (
    "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_PROXY_CONNECTION_FAILED",
    "ERR_TIMED_OUT",
    "ERR_CONNECTION_RESET",
    "ERR_EMPTY_RESPONSE",
)

# Errors that look proxy-related but indicate misconfiguration, not flap.
# Retrying these is pointless and would just waste time + a session slot.
_PERMANENT_PROXY_MARKERS = (
    "ERR_PROXY_AUTH_REQUESTED",
    "ERR_PROXY_AUTH_UNSUPPORTED",
    "ERR_PROXY_CERTIFICATE_INVALID",
    "ERR_MANDATORY_PROXY_CONFIGURATION_FAILED",
)


def is_transient_proxy_error(exc: BaseException) -> bool:
    """True iff ``exc`` looks like a transient proxy/tunnel hiccup worth a
    one-shot retry. Auth/cert errors return False — those won't get better
    on retry and the caller should surface the failure."""
    msg = str(exc)
    if any(m in msg for m in _PERMANENT_PROXY_MARKERS):
        return False
    return any(m in msg for m in _TRANSIENT_PROXY_MARKERS)
