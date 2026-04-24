"""Bot: the attacker side of the contract.

Bot is an abstract base class. Each concrete subclass represents a
different level of sophistication. This mirrors the real arms race:
  - NaiveBot:   a curl-like loop. Always detected.
  - StealthBot: rotates identity and mimics human timing.

The point of this hierarchy is to make the firewall's assumptions
testable. If a new kind of bot evades detection, we add a subclass
and a test; the test either exposes a hole in Firewall or confirms
that the new attack really is outside the current defense model.
"""
from __future__ import annotations

import random
from abc import ABC, abstractmethod
from typing import Iterator

from viewcount.event import ViewEvent


class Bot(ABC):
    """Abstract attacker. Emits a sequence of ViewEvents."""

    def __init__(self, target_post: str, hits: int) -> None:
        self._target_post = target_post
        self._hits = hits

    @abstractmethod
    def attack(self) -> Iterator[ViewEvent]:
        """Yield view events targeting the configured post."""


class NaiveBot(Bot):
    """A script-kiddie bot. Same session, same JA4, no mouse events."""

    def attack(self) -> Iterator[ViewEvent]:
        for i in range(self._hits):
            yield ViewEvent(
                post_id=self._target_post,
                ip="10.0.0.5",                        # data-center IP
                user_agent="python-requests/2.31.0",  # self-identifying
                ja4="t13d0000h1_python_requests",     # bot TLS signature
                session_id="bot-session",             # never rotated
                dwell_ms=100,                         # no real dwell
                mouse_events=0,                       # no interaction
            )


class StealthBot(Bot):
    """A sophisticated bot. Rotates identity, simulates human timing.

    This bot passes the naive checks. Catching it requires behavioral
    biometrics or cross-session pattern analysis -- beyond the scope
    of this teaching firewall. The point is to show that the contract
    must be extensible: Firewall should accept new rules without
    rewriting itself.
    """

    _RESIDENTIAL_PREFIXES = ("203.0.113.", "198.51.100.", "192.0.2.")
    _REAL_USER_AGENTS = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    )

    def attack(self) -> Iterator[ViewEvent]:
        rng = random.Random(42)
        for i in range(self._hits):
            yield ViewEvent(
                post_id=self._target_post,
                ip=self._RESIDENTIAL_PREFIXES[i % 3] + str(rng.randint(1, 254)),
                user_agent=rng.choice(self._REAL_USER_AGENTS),
                ja4="t13d1516h2_chrome_like",         # matches the UA
                session_id=f"sess-{i}-{rng.randint(1000, 9999)}",
                dwell_ms=rng.randint(3000, 12_000),   # plausible dwell
                mouse_events=rng.randint(5, 30),      # fake interaction
            )
