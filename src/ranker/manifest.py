"""DSL manifest schema — the declarative job definition for ranker."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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
    sections: SectionLocators
    scan_depth: Annotated[int, Field(ge=1, le=100)] = 10


class Matching(BaseModel):
    model_config = ConfigDict(extra="forbid")
    by: MatchBy = MatchBy.BLOG_ID


class TargetItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    blog_id: str
    keyword: str
    title: str
    published_at: str = ""


class Targets(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: TargetSourceKind
    path: Path | None = None
    items: list[TargetItem] | None = None

    @model_validator(mode="after")
    def _require_source_fields(self) -> Targets:
        if self.source == TargetSourceKind.FILE and self.path is None:
            raise ValueError("targets.path is required when source is 'file'")
        if self.source == TargetSourceKind.INLINE and not self.items:
            raise ValueError("targets.items is required when source is 'inline'")
        return self


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
    viewport: Viewport = Field(default_factory=Viewport)
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


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1]
    schedule: Schedule
    source: Source
    targets: Targets
    output: Output
    matching: Matching = Field(default_factory=Matching)
    behavior: Behavior = Field(default_factory=Behavior)
    identity: Identity = Field(default_factory=Identity)
    post_visit: PostVisit = Field(default_factory=PostVisit)


def load_manifest(path: Path) -> Manifest:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return Manifest.model_validate(data)
