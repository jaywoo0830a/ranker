"""Immutable data objects that flow through the pipeline."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RankQuery:
    """A single target to look up on Naver."""
    blog_id: str
    keyword: str
    title: str
    published_at: str = ""


@dataclass(frozen=True)
class RankResult:
    """Outcome of a single rank lookup.

    `rank` is 1-indexed position inside the source block, or None when not
    found. `reason` mirrors the reference material's auditability contract —
    every outcome carries a human-readable explanation.
    """
    blog_id: str
    keyword: str
    title: str
    checked_at: str
    rank: int | None
    reason: str
