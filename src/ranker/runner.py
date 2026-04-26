"""Top-level orchestration: schedule loop, target iteration, output persistence."""

from __future__ import annotations

import asyncio
import contextvars
import os
import random
import sys
from contextlib import suppress
from dataclasses import replace
from pathlib import Path

import yaml
from playwright.async_api import async_playwright, Browser

from .behavior import Human
from .event import RankQuery, RankResult, VisitResult
from .identity import ContextPool
from .manifest import (
    IntRange,
    Manifest,
    Mode,
    OutputMode,
    PostVisit,
    ResolvedJob,
    ScrollPolicy,
    TargetItem,
    TargetSourceKind,
    resolve_jobs,
)
from .profile import profile_for
from .proxy import is_transient_proxy_error, require_credentials
from .search import NaverSearch




# Set to the active Job's name during that Job's execution so all log
# lines from helpers (search, retry, etc.) get a consistent ``[name]``
# prefix without threading the name through every signature. ContextVar
# rather than a plain global so Phase-2 parallel Jobs each see their own
# value (asyncio.Tasks copy the parent context at creation time).
_current_job: contextvars.ContextVar[str] = contextvars.ContextVar("job", default="")


def _log(msg: str) -> None:
    job = _current_job.get()
    prefix = f"[{job}] " if job else ""
    print(f"[ranker] {prefix}{msg}", file=sys.stderr, flush=True)


def load_targets(manifest: Manifest) -> list[RankQuery]:
    """Resolve the ``targets`` block to concrete RankQuery objects."""
    if manifest.targets.source == TargetSourceKind.INLINE:
        items = manifest.targets.items or []
    else:
        raw = yaml.safe_load(Path(manifest.targets.path).read_text(encoding="utf-8")) or []
        items = [TargetItem.model_validate(item) for item in raw]

    return [
        RankQuery(
            blog_id=item.blog_id,
            keyword=item.keyword,
            title=item.title,
            published_at=item.published_at,
        )
        for item in items
    ]


def _query_key(q: RankQuery) -> tuple[str, str, str]:
    return (q.blog_id, q.keyword, q.title)


def _load_existing_output(path: Path) -> list[dict]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return data if isinstance(data, list) else []


def _result_to_run_dict(r: RankResult) -> dict:
    """Flatten a RankResult into the per-run YAML shape.

    Optional fields (``url``, ``visit``) are omitted when absent so the disk
    format stays compact for rank-only runs.
    """
    run: dict = {
        "checked_at": r.checked_at,
        "section": r.section,
        "rank": r.rank,
        "reason": r.reason,
    }
    if r.url is not None:
        run["url"] = r.url
    if r.visit is not None:
        visit: dict = {
            "visited_at": r.visit.visited_at,
            "dwelled_ms": r.visit.dwelled_ms,
        }
        if r.visit.engagement is not None:
            eng = r.visit.engagement
            visit["engagement"] = {
                "views": eng.views,
                "likes": eng.likes,
                "comments": eng.comments,
            }
        run["visit"] = visit
    return run


def persist_results(
    manifest: Manifest,
    results: list[RankResult],
    job_name: str,
    mode: Mode,
    *,
    append_only: bool = False,
) -> None:
    """Append new results to the output file, grouped by target.

    Each run dict carries ``job`` and ``mode`` tags so multiple Jobs
    measuring the same target stay distinguishable in post-processing
    (desktop rank vs mobile rank, IP-N rank vs IP-M rank, etc).

    ``append_only`` lets the runner preserve earlier Jobs' results within
    the same scheduled run when ``manifest.output.mode == "overwrite"``:
    only the FIRST persist of a run honors the overwrite policy; later
    Jobs in that run pass ``append_only=True`` so they accumulate instead
    of clobbering.

    Shape on disk::

        - blog_id: ...
          keyword: ...
          title: ...
          runs:
            - job: desktop-1
              mode: desktop
              checked_at: ...
              rank: 5
              reason: matched by blog_id
    """
    output_path = Path(manifest.output.path)

    if not append_only and manifest.output.mode == OutputMode.OVERWRITE:
        existing: list[dict] = []
    else:
        existing = _load_existing_output(output_path)

    index: dict[tuple[str, str, str], dict] = {}
    for row in existing:
        key = (row.get("blog_id", ""), row.get("keyword", ""), row.get("title", ""))
        row.setdefault("runs", [])
        index[key] = row

    for r in results:
        key = (r.blog_id, r.keyword, r.title)
        entry = index.get(key)
        if entry is None:
            entry = {
                "blog_id": r.blog_id,
                "keyword": r.keyword,
                "title": r.title,
                "runs": [],
            }
            index[key] = entry
        # job/mode appear first in the run dict so a glance at the YAML
        # shows which identity produced each measurement.
        run_dict = {"job": job_name, "mode": mode.value, **_result_to_run_dict(r)}
        entry["runs"].append(run_dict)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        yaml.safe_dump(list(index.values()), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


async def _look_up_with_retry(
    pool: ContextPool, search: NaverSearch, context, query: RankQuery,
):
    """Run search.look_up; on transient proxy failure, force-rotate the
    sticky session (= new IP) and retry exactly once. Returns the result
    plus the (possibly new) context so the caller can chain visit_post on
    the same identity."""
    try:
        return context, await search.look_up(context, query)
    except Exception as e:
        if not is_transient_proxy_error(e):
            raise
        _log(f"  ⚠ transient proxy failure on look_up ({type(e).__name__}); rotating SID and retrying")
        new_ctx = await pool.force_rotate()
        return new_ctx, await search.look_up(new_ctx, query)


async def _visit_with_retry(
    pool: ContextPool,
    search: NaverSearch,
    context,
    url: str,
    dwell_ms: IntRange,
    mouse_events: IntRange,
    scroll: ScrollPolicy,
) -> VisitResult:
    try:
        return await search.visit_post(context, url, dwell_ms, mouse_events, scroll)
    except Exception as e:
        if not is_transient_proxy_error(e):
            raise
        _log(f"  ⚠ transient proxy failure on visit ({type(e).__name__}); rotating SID and retrying")
        new_ctx = await pool.force_rotate()
        return await search.visit_post(new_ctx, url, dwell_ms, mouse_events, scroll)


async def _run_once(
    pool: ContextPool,
    search: NaverSearch,
    human: Human,
    queries: list[RankQuery],
    post_visit: PostVisit,
) -> list[RankResult]:
    results: list[RankResult] = []
    await pool.begin_run()
    total = len(queries)
    for idx, query in enumerate(queries):
        _log(f"target {idx + 1}/{total}: '{query.keyword}' for blog_id={query.blog_id}")
        context = await pool.begin_target()
        context, result = await _look_up_with_retry(pool, search, context, query)

        if result.rank is not None:
            _log(f"  → {result.section} rank {result.rank}")
        else:
            _log(f"  → not found ({result.reason})")

        if post_visit.enabled and result.rank is not None and result.url:
            _log("  visiting post, dwelling…")
            visit = await _visit_with_retry(
                pool, search, context, result.url,
                post_visit.dwell_ms, post_visit.mouse_events, post_visit.scroll,
            )
            result = replace(result, visit=visit)
            _log(f"  dwelled {visit.dwelled_ms}ms")

        results.append(result)
        if idx < total - 1:
            _log("  inter-search pause…")
            await human.inter_search_pause()
    return results


async def _run_one_job(
    browser: Browser,
    manifest: Manifest,
    queries: list[RankQuery],
    output_lock: asyncio.Lock,
    job: ResolvedJob,
    account_stem: str | None,
    password: str | None,
) -> None:
    """One Job's full pass over all targets, end-to-end.

    Owns its own ContextPool / Human / NaverSearch. Each Job reads the
    *same* manifest but binds to its *own* (mode, profile, sticky proxy
    session) — so concurrently-running Jobs land on independent IPs and
    fingerprints from Naver's point of view.

    Started by :func:`run` via ``asyncio.gather`` with
    ``return_exceptions=True``, so any unhandled error here surfaces in
    the caller's failure list rather than tearing down peer Jobs.
    """
    # Each Task gets its own ContextVar copy, so this only labels logs
    # from THIS Job's task — peers keep their own prefix.
    _current_job.set(job.name)

    # Stagger the burst — without this, N parallel Jobs hit Naver search
    # within ms of each other, which itself is a detection signal. The
    # jitter window follows the manifest's pacing intent: dev manifests
    # with tiny ``inter_search_s`` get tiny jitter, prod manifests with
    # 30-90s pacing get matching jitter.
    jitter_window = float(manifest.behavior.inter_search_s.max)
    jitter = random.uniform(0, jitter_window)
    if jitter > 0.5:
        _log(f"start jitter: sleeping {jitter:.1f}s")
        await asyncio.sleep(jitter)

    profile = profile_for(job.mode)
    pool = ContextPool(
        browser, manifest.identity, profile,
        proxy=manifest.proxy,
        account_stem=account_stem,
        password=password,
    )
    human = Human(manifest.behavior)
    search = NaverSearch(manifest.source, manifest.matching, human, profile)
    _log(f"mode={job.mode.value}, beginning {len(queries)} target(s)")
    try:
        results = await _run_once(pool, search, human, queries, manifest.post_visit)
        # Serialize file writes — read-modify-write of the YAML output is
        # not safe under concurrent persists, even within asyncio. The lock
        # is held only for the brief I/O window so contention is minimal.
        async with output_lock:
            # Overwrite is handled once per run before Jobs spawn (see run()),
            # so every Job here appends.
            persist_results(
                manifest, results, job.name, job.mode, append_only=True,
            )
        _log(f"persisted {len(results)} target(s) to {manifest.output.path}")
    finally:
        with suppress(Exception):
            await pool.close()


def _truncate_output(manifest: Manifest) -> None:
    """Reset the output file to an empty list. Called once at the start of
    each run when ``output.mode == "overwrite"``, since concurrent Jobs
    can't safely each take overwrite semantics — exactly one party must
    decide "the file starts here."""
    output_path = Path(manifest.output.path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("[]\n", encoding="utf-8")


async def run(manifest: Manifest) -> None:
    queries = load_targets(manifest)
    interval = manifest.schedule.interval_delta
    total_runs = manifest.schedule.count
    jobs = resolve_jobs(manifest)

    # Fail fast on missing credentials — we'd rather error before launching
    # Chromium than three minutes into a run. Credentials are shared across
    # Jobs (one ProxyEmpire account, many SIDs), so this is a single check.
    account_stem: str | None = None
    password: str | None = None
    if manifest.proxy is not None:
        account_stem, password = require_credentials(manifest.proxy.provider)
        _log(
            f"proxy: {manifest.proxy.provider.value} "
            f"({manifest.proxy.host}:{manifest.proxy.port}, "
            f"country={manifest.proxy.country}, "
            f"sticky {manifest.proxy.session_ttl_minutes}m)"
        )
    else:
        _log("proxy: none (direct connection)")

    job_summary = ", ".join(f"{j.name}({j.mode.value})" for j in jobs)
    _log(f"jobs: {len(jobs)} (parallel) — {job_summary}")

    output_lock = asyncio.Lock()

    async with async_playwright() as pw:
        headless = os.environ.get("RANKER_HEADFUL", "").lower() not in ("1", "true", "yes")
        browser = await pw.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )

        try:
            for run_idx in range(total_runs):
                _log(f"run {run_idx + 1}/{total_runs}")
                # Apply the overwrite policy ONCE per run before Jobs
                # spawn — concurrent Jobs all use append semantics so they
                # don't clobber each other.
                if manifest.output.mode == OutputMode.OVERWRITE:
                    _truncate_output(manifest)

                tasks = [
                    _run_one_job(
                        browser, manifest, queries, output_lock,
                        job, account_stem, password,
                    )
                    for job in jobs
                ]
                outcomes = await asyncio.gather(*tasks, return_exceptions=True)

                # Surface per-Job failures without tearing down the run.
                # Transient proxy errors are already caught one level down
                # (look_up/visit retry); anything reaching here is a real
                # failure worth flagging.
                for job, outcome in zip(jobs, outcomes):
                    if isinstance(outcome, BaseException):
                        _log(
                            f"⚠ job {job.name} failed: "
                            f"{type(outcome).__name__}: {outcome}"
                        )

                if run_idx < total_runs - 1:
                    _log(f"sleeping {int(interval.total_seconds())}s until next run")
                    await asyncio.sleep(interval.total_seconds())
        finally:
            # Ctrl-C kills the driver before these run; suppressing the
            # inevitable "Connection closed" noise keeps exits clean.
            with suppress(Exception):
                await browser.close()
