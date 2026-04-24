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
class Engagement:
    """Interaction metrics scraped from a blog post.

    Each field is nullable because the scraper may not always locate the
    element (page variants, iframe load failures, etc). Reserved as a
    placeholder now; populated once the engagement extractor lands.
    """
    views: int | None = None
    likes: int | None = None
    comments: int | None = None


@dataclass(frozen=True)
class VisitResult:
    """Outcome of navigating to a ranked post and dwelling on it.

    ``engagement`` is an extension point — initially always ``None``; when
    the engagement extractor lands it will be populated without changing the
    rest of the pipeline or the output YAML contract.
    """
    visited_at: str
    url: str
    dwelled_ms: int
    engagement: Engagement | None = None


@dataclass(frozen=True)
class RankResult:
    """Outcome of a single rank lookup.

    ``section`` names the container the post was found in (``head`` / ``body``)
    or ``None`` when absent. ``rank`` is 1-indexed within that section, or
    ``None`` when absent. ``url`` is the matched post's URL (``None`` on miss).
    ``visit`` carries the visit outcome when ``post_visit`` is enabled and a
    rank was found, otherwise ``None``. ``reason`` mirrors the reference
    material's auditability contract — every outcome carries a human-readable
    explanation.
    """
    blog_id: str
    keyword: str
    title: str
    checked_at: str
    section: str | None
    rank: int | None
    url: str | None
    reason: str
    visit: VisitResult | None = None
