"""ViewEvent: the unit of traffic the firewall inspects.

An immutable value object. Carries both network-layer signals
(IP, User-Agent, JA4) and behavior-layer signals (dwell time,
mouse events) so downstream rules can reason about either.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ViewEvent:
    """A single page-view request with its attached signals."""

    post_id: str
    ip: str
    user_agent: str
    ja4: str
    session_id: str
    dwell_ms: int
    mouse_events: int
