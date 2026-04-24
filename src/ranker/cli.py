"""Entry point: ``ranker <manifest.yaml>``.

Intentionally thin — the DSL manifest is the interface. The only job of the
CLI is to locate, load, validate, and hand off to the runner.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from .manifest import load_manifest
from .runner import run


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or args[0] in ("-h", "--help"):
        print("usage: ranker <manifest.yaml>", file=sys.stderr)
        return 2

    manifest = load_manifest(Path(args[0]))
    try:
        asyncio.run(run(manifest))
    except KeyboardInterrupt:
        print("interrupted; partial results are already persisted.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
