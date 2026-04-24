"""Contract tests for the defense (Firewall) and offense (Bot).

These tests encode the object-oriented contracts described in the book.
They are written first (TDD) and drive the implementation in src/.
"""
from __future__ import annotations

import pytest

from viewcount.event import ViewEvent
from viewcount.firewall import Firewall, Verdict
from viewcount.bot import Bot, NaiveBot, StealthBot
from viewcount.identity import IdentityExtractor


# ---------------------------------------------------------------------------
# Contract 1: ViewEvent is an immutable data carrier.
# ---------------------------------------------------------------------------

class TestViewEventContract:
    def test_event_exposes_network_and_behavior_fields(self):
        event = ViewEvent(
            post_id="p1",
            ip="1.2.3.4",
            user_agent="Mozilla/5.0",
            ja4="t13d1516h2_abc_def",
            session_id="s1",
            dwell_ms=5000,
            mouse_events=12,
        )
        assert event.post_id == "p1"
        assert event.ip == "1.2.3.4"
        assert event.ja4.startswith("t13")
        assert event.dwell_ms == 5000

    def test_event_is_frozen(self):
        event = ViewEvent(
            post_id="p1", ip="1.1.1.1", user_agent="ua",
            ja4="ja4", session_id="s", dwell_ms=4000, mouse_events=3,
        )
        with pytest.raises(Exception):
            event.post_id = "p2"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Contract 2: IdentityExtractor produces a stable fingerprint.
# ---------------------------------------------------------------------------

class TestIdentityExtractorContract:
    def test_same_session_produces_same_fingerprint(self):
        extractor = IdentityExtractor()
        event_a = _make_event(session_id="sA", ja4="ja4-x")
        event_b = _make_event(session_id="sA", ja4="ja4-x")
        assert extractor.fingerprint(event_a) == extractor.fingerprint(event_b)

    def test_different_ja4_produces_different_fingerprint(self):
        extractor = IdentityExtractor()
        event_a = _make_event(session_id="sA", ja4="ja4-x")
        event_b = _make_event(session_id="sA", ja4="ja4-y")
        assert extractor.fingerprint(event_a) != extractor.fingerprint(event_b)


# ---------------------------------------------------------------------------
# Contract 3: Firewall returns a Verdict (ACCEPT | SUSPICIOUS | REJECT).
# A Verdict is never a bare boolean: multi-layer defense needs gradations.
# ---------------------------------------------------------------------------

class TestFirewallContract:
    def test_firewall_accepts_human_like_event(self):
        firewall = Firewall()
        event = _make_event(
            ip="203.0.113.10",         # residential-looking
            user_agent="Mozilla/5.0 (Macintosh)",
            ja4="t13d1516h2_chrome_like",
            dwell_ms=8000,
            mouse_events=20,
        )
        assert firewall.evaluate(event).decision == Verdict.ACCEPT

    def test_firewall_rejects_datacenter_ip(self):
        firewall = Firewall()
        event = _make_event(ip="10.0.0.5")  # private/DC range
        assert firewall.evaluate(event).decision == Verdict.REJECT

    def test_firewall_rejects_ja4_ua_mismatch(self):
        firewall = Firewall()
        event = _make_event(
            user_agent="Mozilla/5.0 (Chrome/120)",
            ja4="t13d0000h1_python_requests",  # known bot signature
        )
        assert firewall.evaluate(event).decision == Verdict.REJECT

    def test_firewall_flags_no_mouse_events(self):
        firewall = Firewall()
        event = _make_event(mouse_events=0, dwell_ms=35_000)
        result = firewall.evaluate(event)
        assert result.decision in (Verdict.SUSPICIOUS, Verdict.REJECT)

    def test_firewall_rejects_burst_from_same_fingerprint(self):
        firewall = Firewall()
        event = _make_event(session_id="burst-1")
        for _ in range(5):
            firewall.evaluate(event)
        # 6th hit within window should be suspicious or rejected
        final = firewall.evaluate(event)
        assert final.decision != Verdict.ACCEPT

    def test_verdict_carries_human_readable_reason(self):
        firewall = Firewall()
        event = _make_event(ip="10.0.0.5")
        result = firewall.evaluate(event)
        assert isinstance(result.reason, str)
        assert len(result.reason) > 0


# ---------------------------------------------------------------------------
# Contract 4: Bot is an abstract attacker. Concrete bots emit ViewEvents.
# ---------------------------------------------------------------------------

class TestBotContract:
    def test_bot_is_abstract(self):
        with pytest.raises(TypeError):
            Bot()  # type: ignore[abstract]

    def test_naive_bot_produces_events(self):
        bot = NaiveBot(target_post="p1", hits=3)
        events = list(bot.attack())
        assert len(events) == 3
        assert all(e.post_id == "p1" for e in events)

    def test_naive_bot_is_detectable(self):
        firewall = Firewall()
        bot = NaiveBot(target_post="p1", hits=5)
        verdicts = [firewall.evaluate(e).decision for e in bot.attack()]
        # At least half should not be accepted
        rejected = [v for v in verdicts if v != Verdict.ACCEPT]
        assert len(rejected) >= len(verdicts) // 2

    def test_stealth_bot_is_harder_to_detect(self):
        firewall_a = Firewall()
        firewall_b = Firewall()
        naive = NaiveBot(target_post="p1", hits=10)
        stealth = StealthBot(target_post="p1", hits=10)
        naive_rejects = sum(
            1 for e in naive.attack()
            if firewall_a.evaluate(e).decision != Verdict.ACCEPT
        )
        stealth_rejects = sum(
            1 for e in stealth.attack()
            if firewall_b.evaluate(e).decision != Verdict.ACCEPT
        )
        assert stealth_rejects < naive_rejects


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event(**overrides) -> ViewEvent:
    defaults = dict(
        post_id="p1",
        ip="203.0.113.10",
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        ja4="t13d1516h2_chrome_like",
        session_id="session-default",
        dwell_ms=6000,
        mouse_events=10,
    )
    defaults.update(overrides)
    return ViewEvent(**defaults)
