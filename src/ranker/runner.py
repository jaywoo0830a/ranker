"""Top-level orchestration: schedule loop, target iteration, output persistence."""

from __future__ import annotations

import asyncio
from pathlib import Path

import yaml
from playwright.async_api import async_playwright

from .behavior import Human
from .event import RankQuery, RankResult
from .identity import ContextPool
from .manifest import Manifest, OutputMode, TargetItem, TargetSourceKind
from .search import NaverSearch


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


def persist_results(manifest: Manifest, results: list[RankResult]) -> None:
    """Append new results to the output file, grouped by target.

    Shape on disk::

        - blog_id: ...
          keyword: ...
          title: ...
          runs:
            - checked_at: ...
              rank: 5
              reason: matched by blog_id
    """
    output_path = Path(manifest.output.path)

    if manifest.output.mode == OutputMode.OVERWRITE:
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
        entry["runs"].append({
            "checked_at": r.checked_at,
            "rank": r.rank,
            "reason": r.reason,
        })

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        yaml.safe_dump(list(index.values()), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


async def _run_once(
    pool: ContextPool,
    search: NaverSearch,
    human: Human,
    queries: list[RankQuery],
) -> list[RankResult]:
    results: list[RankResult] = []
    await pool.begin_run()
    for idx, query in enumerate(queries):
        context = await pool.begin_target()
        result = await search.look_up(context, query)
        results.append(result)
        if idx < len(queries) - 1:
            await human.inter_search_pause()
    return results


async def run(manifest: Manifest) -> None:
    queries = load_targets(manifest)
    interval = manifest.schedule.interval_delta
    total_runs = manifest.schedule.count

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        pool = ContextPool(browser, manifest.identity)
        human = Human(manifest.behavior)
        search = NaverSearch(manifest.source, manifest.matching, human)

        try:
            for run_idx in range(total_runs):
                results = await _run_once(pool, search, human, queries)
                persist_results(manifest, results)
                if run_idx < total_runs - 1:
                    await asyncio.sleep(interval.total_seconds())
        finally:
            await pool.close()
            await browser.close()
