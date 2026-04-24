#!/usr/bin/env bash
# dev.sh - run ranker against a manifest
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"

MANIFEST="${1:-examples/job.yaml}"

echo "[dev] manifest: $MANIFEST"
uv run ranker "$MANIFEST"
