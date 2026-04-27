"""HTTP API + subprocess orchestration around the ranker CLI.

The CLI itself (``ranker``) stays the workhorse — it knows how to drive
Playwright, parse Naver search, and persist results. This package wraps
it: each ``POST /api/jobs`` writes a per-job directory to disk and spawns
``ranker {manifest.yaml}`` as a subprocess. State is tracked via a small
JSON file per job so the server can restart without losing track of
finished work.
"""
