"""Entry point: ``ranker <manifest.yaml>``.

Intentionally thin — the DSL manifest is the interface. The only job of the
CLI is to locate, load, validate, and hand off to the runner.

Shutdown semantics:
    SIGINT (Ctrl+C) and SIGTERM (``kill <pid>`` or parent-driven shutdown
    from JobManager / uvicorn lifespan) both unwind through the same
    KeyboardInterrupt path, so the runner's ``finally`` chain runs —
    closing the BrowserContext, the Playwright driver, and Chromium.
    Without this, SIGTERM kills Python instantly and leaves the
    Playwright Node driver + headless Chromium orphaned.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

from .manifest import load_manifest
from .runner import run


def _install_sigterm_handler() -> None:
    """Convert SIGTERM into KeyboardInterrupt.

    Python's default SIGTERM action terminates the process immediately
    with no cleanup — that's how we ended up with orphaned headless
    Chromium processes. Re-raising as KeyboardInterrupt routes through
    the ``except`` block in ``main()`` and lets every running task's
    ``finally`` clause execute, including ``await browser.close()``
    which signals the Playwright driver to terminate cleanly.
    """
    def _handler(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _handler)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or args[0] in ("-h", "--help"):
        print("usage: ranker <manifest.yaml>", file=sys.stderr)
        return 2

    _install_sigterm_handler()

    manifest = load_manifest(Path(args[0]))
    try:
        asyncio.run(run(manifest))
    except KeyboardInterrupt:
        print("interrupted; partial results are already persisted.", file=sys.stderr)
        # Convention: 130 = process killed by signal during graceful
        # shutdown. JobManager treats this as ``cancelled`` (not
        # ``failed``) — see ranker_service.jobs._exec.
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
