"""IdentityExtractor: derive a stable fingerprint from a ViewEvent.

The fingerprint is intentionally session_id + JA4, not IP.
IP is volatile on mobile NAT; JA4 is stable across IP changes.
"""
from __future__ import annotations

import hashlib

from viewcount.event import ViewEvent


class IdentityExtractor:
    """Turns a ViewEvent into an opaque fingerprint string."""

    def fingerprint(self, event: ViewEvent) -> str:
        raw = f"{event.session_id}|{event.ja4}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
