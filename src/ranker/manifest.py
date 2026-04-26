"""DSL manifest schema — the declarative job definition for ranker."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Mode(str, Enum):
    DESKTOP = "desktop"
    MOBILE = "mobile"


class LocatorType(str, Enum):
    CSS = "css"
    XPATH = "xpath"


class SourceKind(str, Enum):
    NAVER_UNIFIED_SEARCH = "naver_unified_search"


class MatchBy(str, Enum):
    BLOG_ID = "blog_id"
    TITLE = "title"
    BLOG_ID_AND_TITLE = "blog_id_and_title"


class TargetSourceKind(str, Enum):
    FILE = "file"
    INLINE = "inline"


class OutputMode(str, Enum):
    APPEND = "append"
    OVERWRITE = "overwrite"


class RotatePolicy(str, Enum):
    PER_RUN = "per_run"
    PER_TARGET = "per_target"
    NEVER = "never"


class ProxyProvider(str, Enum):
    PROXYEMPIRE = "proxyempire"


class ResourceType(str, Enum):
    """Subset of Playwright resource types we expose for blocking.

    Limited to types whose absence doesn't break rank parsing or the
    appearance of a real browser session — ``script`` / ``xhr`` / ``fetch``
    are intentionally not options because Naver's dynamic search variants
    rely on them and missing them looks bot-like.
    """
    IMAGE = "image"
    FONT = "font"
    MEDIA = "media"
    STYLESHEET = "stylesheet"


_DURATION_UNITS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
}


def parse_duration(spec: str) -> timedelta:
    """Parse '60min', '30s', '1h', '2h30m' into a timedelta."""
    pairs = re.findall(r"(\d+)\s*([a-z]+)", spec.strip().lower())
    if not pairs:
        raise ValueError(f"invalid duration: {spec!r}")
    total = 0
    for num, unit in pairs:
        if unit not in _DURATION_UNITS:
            raise ValueError(f"unknown duration unit {unit!r} in {spec!r}")
        total += int(num) * _DURATION_UNITS[unit]
    return timedelta(seconds=total)


class Locator(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: LocatorType
    value: str


class Schedule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    count: Annotated[int, Field(ge=1)]
    interval: str
    start: str | datetime = "immediate"

    @field_validator("interval")
    @classmethod
    def _check_interval(cls, v: str) -> str:
        parse_duration(v)
        return v

    @field_validator("start", mode="before")
    @classmethod
    def _coerce_start(cls, v):
        if isinstance(v, datetime) or v == "immediate":
            return v
        if isinstance(v, str):
            return datetime.fromisoformat(v)
        raise ValueError(f"invalid start: {v!r}")

    @property
    def interval_delta(self) -> timedelta:
        return parse_duration(self.interval)


class SectionLocators(BaseModel):
    """Locators for the two blocks Naver renders in unified search.

    ``head`` is the rerank-head (top highlighted) block; ``body`` is the main
    results block. Rank is reported per section so "appeared in head" and
    "appeared in body" stay distinguishable.
    """
    model_config = ConfigDict(extra="forbid")
    head: Locator
    body: Locator


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: SourceKind
    # When omitted, the active mode's profile supplies the defaults — so a
    # simple ``mode: mobile`` manifest just works without selector copy-paste.
    sections: SectionLocators | None = None
    scan_depth: Annotated[int, Field(ge=1, le=100)] = 10


class Matching(BaseModel):
    model_config = ConfigDict(extra="forbid")
    by: MatchBy = MatchBy.BLOG_ID


class TargetItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    blog_id: str
    keyword: str
    title: str
    # Empty → no scheduled-publish gating (target is always eligible).
    # Otherwise must be ISO 8601 with timezone offset, e.g.
    # ``2026-04-27T15:00:00+09:00``. Naive datetimes are rejected because
    # mixing them with the runner's tz-aware "now" is silently wrong.
    published_at: str = ""

    @field_validator("published_at")
    @classmethod
    def _check_iso(cls, v: str) -> str:
        if not v:
            return v
        try:
            dt = datetime.fromisoformat(v)
        except ValueError as e:
            raise ValueError(
                f"published_at must be ISO 8601 datetime, got {v!r}"
            ) from e
        if dt.tzinfo is None:
            raise ValueError(
                f"published_at must include timezone offset (e.g. +09:00), got {v!r}"
            )
        return v


class Targets(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: TargetSourceKind
    path: Path | None = None
    items: list[TargetItem] | None = None
    # Skip rank lookup for any target whose ``published_at + this_delay``
    # is still in the future. Naver needs lead time to index a freshly
    # published post, so checking too early gets nothing but "not found"
    # and burns a search slot. 60min is a comfortable cushion; set
    # ``"0s"`` to disable gating entirely. Has no effect on targets with
    # empty ``published_at``.
    lookup_delay_after_published: str = "60min"

    @field_validator("lookup_delay_after_published")
    @classmethod
    def _check_delay(cls, v: str) -> str:
        parse_duration(v)
        return v

    @model_validator(mode="after")
    def _require_source_fields(self) -> Targets:
        if self.source == TargetSourceKind.FILE and self.path is None:
            raise ValueError("targets.path is required when source is 'file'")
        if self.source == TargetSourceKind.INLINE and not self.items:
            raise ValueError("targets.items is required when source is 'inline'")
        return self

    @property
    def lookup_delay_delta(self) -> timedelta:
        return parse_duration(self.lookup_delay_after_published)


class Output(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: Path
    mode: OutputMode = OutputMode.APPEND


class IntRange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min: Annotated[int, Field(ge=0)]
    max: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def _check_order(self) -> IntRange:
        if self.min > self.max:
            raise ValueError(f"min ({self.min}) must be <= max ({self.max})")
        return self


class FloatRange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min: Annotated[float, Field(ge=0.0, le=1.0)]
    max: Annotated[float, Field(ge=0.0, le=1.0)]

    @model_validator(mode="after")
    def _check_order(self) -> FloatRange:
        if self.min > self.max:
            raise ValueError(f"min ({self.min}) must be <= max ({self.max})")
        return self


class Behavior(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # 10s floor satisfies the Firewall's behavior-layer rule (dwell >= 500ms
    # is the bare minimum; 10s is what actually reads as human).
    dwell_ms: IntRange = Field(default_factory=lambda: IntRange(min=10000, max=20000))
    mouse_events: IntRange = Field(default_factory=lambda: IntRange(min=5, max=15))
    keystroke_delay_ms: IntRange = Field(default_factory=lambda: IntRange(min=80, max=180))
    inter_search_s: IntRange = Field(default_factory=lambda: IntRange(min=30, max=90))


class Viewport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    width: Annotated[int, Field(ge=320, le=4096)] = 1280
    height: Annotated[int, Field(ge=240, le=4096)] = 800


class Identity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    locale: str = "ko-KR"
    timezone: str = "Asia/Seoul"
    # None → the active mode's profile supplies the viewport.
    viewport: Viewport | None = None
    rotate_context: RotatePolicy = RotatePolicy.PER_RUN


class ScrollPolicy(BaseModel):
    """How far through the page the reader scrolls.

    ``depth_ratio`` : fraction of the scrollable content the reader reaches
                      by the end of their dwell. 0.8 means "scrolled to ~80%
                      of the post". A range lets every visit land at a
                      slightly different finishing point.
    ``down_bias``   : among individual scroll nudges, fraction that go DOWN.
                      0.85 default leaves room for the occasional re-read;
                      1.0 is monotone downward (unnatural); 0.5 is symmetric
                      noise.

    Per-nudge step size is derived automatically from ``depth_ratio``,
    ``down_bias`` and the chosen ``mouse_events`` count — the reader ends at
    the targeted depth with natural per-scroll jitter.
    """
    model_config = ConfigDict(extra="forbid")
    depth_ratio: FloatRange = Field(default_factory=lambda: FloatRange(min=0.7, max=0.95))
    down_bias: Annotated[float, Field(ge=0.0, le=1.0)] = 0.85


class PostVisit(BaseModel):
    """Optional: after a rank match, navigate to the blog post and dwell.

    Engagement scraping (views / likes / comments) is a planned extension;
    when it lands it will be an optional sub-section here so the on-disk
    manifest stays backwards compatible.
    """
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    dwell_ms: IntRange = Field(default_factory=lambda: IntRange(min=160000, max=200000))
    mouse_events: IntRange = Field(default_factory=lambda: IntRange(min=20, max=60))
    scroll: ScrollPolicy = Field(default_factory=ScrollPolicy)


class Proxy(BaseModel):
    """Upstream HTTP proxy used for every BrowserContext.

    Absence of this block in the manifest means **no proxy** — useful for
    local development against the real network. When present, credentials
    are loaded from environment variables (per provider), never from the
    manifest itself, so secrets never touch disk or git.

    ProxyEmpire encodes session/region into the username, so ``country``
    and ``session_ttl`` here drive how the runtime composes the final
    proxy username at context-rotation time.
    """
    model_config = ConfigDict(extra="forbid")
    provider: ProxyProvider = ProxyProvider.PROXYEMPIRE
    host: str
    port: Annotated[int, Field(ge=1, le=65535)]
    # ISO 3166-1 alpha-2 country code; ProxyEmpire matches `country-<cc>`
    # in the username. Defaults to KR since this project targets Naver.
    country: str = "kr"
    # Sticky-session TTL. ProxyEmpire's documented ceiling is 60min;
    # 30min comfortably covers a search→post-visit sequence.
    session_ttl: str = "30m"

    @field_validator("session_ttl")
    @classmethod
    def _check_session_ttl(cls, v: str) -> str:
        secs = parse_duration(v).total_seconds()
        if secs < 60:
            raise ValueError(f"session_ttl must be at least 1 minute, got {v!r}")
        if secs > 3600:
            raise ValueError(
                f"session_ttl must be at most 60 minutes (ProxyEmpire limit), got {v!r}"
            )
        return v

    @field_validator("country")
    @classmethod
    def _check_country(cls, v: str) -> str:
        if not (len(v) == 2 and v.isalpha() and v.islower()):
            raise ValueError(f"country must be lowercase ISO 2-letter code, got {v!r}")
        return v

    @property
    def session_ttl_minutes(self) -> int:
        return int(parse_duration(self.session_ttl).total_seconds() // 60)


class Resources(BaseModel):
    """Network-level traffic control.

    When this block is omitted, every request goes through the proxy
    (current default). When present, the listed Playwright resource types
    are aborted at the BrowserContext layer — those requests never leave
    Chromium, so they don't consume proxy bandwidth.

    Blocking ``image``/``font``/``media`` typically cuts 30-50% of bytes
    with no functional impact: rank parsing reads HTML anchors, not pixels;
    post_visit dwell tracks page-open time, not pixel rendering.

    ``block_third_party_trackers`` aborts requests to a curated list of
    well-known ad/analytics hosts. First-party Naver telemetry is always
    allowed, so the session still looks like a normal user (with adblock).
    """
    model_config = ConfigDict(extra="forbid")
    block: list[ResourceType] = Field(default_factory=list)
    block_third_party_trackers: bool = False


class JobOverride(BaseModel):
    """Per-Job override of a small set of manifest defaults.

    A Job is one parallel worker — its own BrowserContext, its own sticky
    proxy session (= its own IP), running independently from peer Jobs.
    Anything not listed here (schedule, source, targets, matching, output,
    proxy, ``rotate_context`` policy, etc.) is shared across all Jobs and
    only configurable at the manifest top level.

    Today the only override is ``mode`` — that covers the primary use case
    (desktop + mobile mix in one run). Per-Job behavior/identity tweaks
    can be added when there's a real need; keeping the surface tiny avoids
    a five-axis merge nobody asked for.
    """
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)
    mode: Mode | None = None


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1]
    mode: Mode = Mode.DESKTOP
    schedule: Schedule
    source: Source
    targets: Targets
    output: Output
    matching: Matching = Field(default_factory=Matching)
    behavior: Behavior = Field(default_factory=Behavior)
    identity: Identity = Field(default_factory=Identity)
    post_visit: PostVisit = Field(default_factory=PostVisit)
    # Absent → direct connection (dev mode). Present → proxy required.
    proxy: Proxy | None = None
    # Absent → no resource blocking (every request reaches the proxy).
    # Present → abort listed types at the BrowserContext layer.
    resources: Resources | None = None
    # Absent → run as a single implicit Job using top-level config
    # (current behavior, fully backward compatible). Present → spawn one
    # Job per entry; each gets its own ContextPool / SID / IP.
    jobs: list[JobOverride] | None = None

    @model_validator(mode="after")
    def _check_unique_job_names(self) -> Manifest:
        if self.jobs:
            names = [j.name for j in self.jobs]
            duplicates = {n for n in names if names.count(n) > 1}
            if duplicates:
                raise ValueError(
                    f"job names must be unique; duplicates: {sorted(duplicates)}"
                )
        return self


@dataclass(frozen=True)
class ResolvedJob:
    """A fully-materialized Job — manifest defaults merged with overrides.

    One ResolvedJob is what a single concurrent worker actually executes.
    Producing these via :func:`resolve_jobs` keeps the runner free of any
    "is this an override or a default?" branching at execution time.
    """
    name: str
    mode: Mode


def resolve_jobs(manifest: Manifest) -> list[ResolvedJob]:
    """Materialize the manifest into a list of ResolvedJob.

    Backward compatibility: when ``manifest.jobs`` is absent, return a
    single implicit Job (named ``"default"``) using the top-level config —
    so existing single-Job manifests don't grow a ``jobs:`` block just to
    keep working.
    """
    if manifest.jobs is None:
        return [ResolvedJob(name="default", mode=manifest.mode)]
    return [
        ResolvedJob(
            name=j.name,
            mode=j.mode if j.mode is not None else manifest.mode,
        )
        for j in manifest.jobs
    ]


def load_manifest(path: Path) -> Manifest:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return Manifest.model_validate(data)
