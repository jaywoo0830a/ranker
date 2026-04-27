"""Pydantic models matching docs/openapi.yaml.

Kept separate from ``ranker.manifest`` because the API surface
(Job/Progress/ConcurrencyInfo) is unrelated to the manifest DSL —
they happen to both use Pydantic but speak different languages.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobMode(str, Enum):
    DESKTOP = "desktop"
    MOBILE = "mobile"


class JobConfig(BaseModel):
    name: str
    mode: JobMode


class Progress(BaseModel):
    completed_runs: int = Field(ge=0)
    total_runs: int = Field(ge=1)
    next_run_at: datetime | None = None


class CacheStats(BaseModel):
    """Disk-cache savings rolled up across all (Job × run) of one job.

    The runner writes ``cache_stats.json`` at end-of-execution; the API
    reads it lazily on every Job fetch so live polling sees the file
    appear (and the totals grow) as runs complete.
    """
    total_hits: int = Field(ge=0)
    total_misses: int = Field(ge=0)
    total_stored: int = Field(ge=0)
    total_bytes_saved: int = Field(ge=0)
    hit_rate: float = Field(ge=0.0, le=1.0)


class Job(BaseModel):
    id: str
    name: str
    status: JobStatus
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    progress: Progress
    jobs_config: list[JobConfig]
    targets_count: int = Field(ge=0)
    queue_position: int | None = None
    error: str | None = None
    # None until the runner writes cache_stats.json (= caching wasn't
    # enabled, or the run hasn't reached the finally block yet).
    cache_stats: CacheStats | None = None


class JobSummary(BaseModel):
    id: str
    name: str
    status: JobStatus
    created_at: datetime
    progress: Progress
    queue_position: int | None = None


class ConcurrencyInfo(BaseModel):
    running: int = Field(ge=0)
    queued: int = Field(ge=0)
    max: int = Field(ge=1)


class JobListResponse(BaseModel):
    jobs: list[JobSummary]
    concurrency: ConcurrencyInfo


class ErrorResponse(BaseModel):
    error: str
    message: str
    details: dict | None = None
