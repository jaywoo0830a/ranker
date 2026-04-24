"""Firewall: the validation layer.

Implements multi-signal defense:
  1. Static signals  (datacenter IP, JA4/UA mismatch)
  2. Behavior signals (no mouse events, impossible dwell)
  3. Aggregate signals (burst detection per fingerprint)

A Verdict is ACCEPT | SUSPICIOUS | REJECT -- never a bare boolean.
The gradation lets upstream rankers weight borderline views less
without dropping them entirely.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from enum import Enum
from typing import Deque

from viewcount.event import ViewEvent
from viewcount.identity import IdentityExtractor


class Verdict(Enum):
    """Three-valued decision produced by the firewall."""

    ACCEPT = "accept"
    SUSPICIOUS = "suspicious"
    REJECT = "reject"


@dataclass(frozen=True)
class Decision:
    """Verdict plus a human-readable reason for auditability."""

    decision: Verdict
    reason: str


# Known data-center / private IP prefixes. Real systems use MaxMind ASN.
_DATACENTER_PREFIXES = ("10.", "172.16.", "192.168.", "169.254.")

# Known bot TLS fingerprints. Real systems maintain a live database.
_BOT_JA4_TOKENS = ("python_requests", "curl", "go_http", "headless")

_BURST_LIMIT = 5
_BURST_WINDOW_SIZE = 5


class Firewall:
    """Multi-signal validator for incoming view events."""

    def __init__(self) -> None:
        self._identity = IdentityExtractor()
        self._recent: dict[str, Deque[ViewEvent]] = defaultdict(
            lambda: deque(maxlen=_BURST_WINDOW_SIZE)
        )

    def evaluate(self, event: ViewEvent) -> Decision:
        """Return a Decision for the event."""
        static = self._check_static(event)
        if static is not None:
            return static

        behavior = self._check_behavior(event)
        if behavior is not None:
            return behavior

        aggregate = self._check_aggregate(event)
        if aggregate is not None:
            return aggregate

        return Decision(Verdict.ACCEPT, "all checks passed")

    # ---- layer 1: static signals ------------------------------------------
    def _check_static(self, event: ViewEvent) -> Decision | None:
        if event.ip.startswith(_DATACENTER_PREFIXES):
            return Decision(Verdict.REJECT, "datacenter or private IP range")
        if self._ja4_ua_mismatch(event):
            return Decision(Verdict.REJECT, "JA4 and User-Agent disagree")
        return None

    @staticmethod
    def _ja4_ua_mismatch(event: ViewEvent) -> bool:
        ua = event.user_agent.lower()
        claims_browser = any(
            token in ua for token in ("chrome", "safari", "firefox", "edge")
        )
        looks_like_bot_tls = any(
            token in event.ja4.lower() for token in _BOT_JA4_TOKENS
        )
        return claims_browser and looks_like_bot_tls

    # ---- layer 2: behavior signals ----------------------------------------
    @staticmethod
    def _check_behavior(event: ViewEvent) -> Decision | None:
        if event.dwell_ms > 30_000 and event.mouse_events == 0:
            return Decision(Verdict.SUSPICIOUS, "long dwell, zero mouse events")
        if event.dwell_ms < 500:
            return Decision(Verdict.SUSPICIOUS, "implausibly short dwell")
        return None

    # ---- layer 3: aggregate signals ---------------------------------------
    def _check_aggregate(self, event: ViewEvent) -> Decision | None:
        fp = self._identity.fingerprint(event)
        window = self._recent[fp]
        window.append(event)
        if len(window) >= _BURST_LIMIT:
            return Decision(
                Verdict.REJECT,
                f"{len(window)} views from same fingerprint in window",
            )
        return None
