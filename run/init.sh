#!/usr/bin/env bash
# init.sh - run once to set up the environment
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"

echo "[init] Syncing dependencies with uv..."
uv sync

echo "[init] Installing Playwright Chromium..."
uv run playwright install chromium

echo "[init] Done."
echo "[init] Next: ./run/dev.sh examples/job.yaml"
