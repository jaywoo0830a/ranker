#!/usr/bin/env bash
# dev.sh - activate dev environment and serve the book
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"

if [ -d ".venv" ]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
fi

export PYTHONPATH="$PROJECT_ROOT/src:${PYTHONPATH:-}"

echo "[dev] Python: $(python --version)"
echo "[dev] Project: $PROJECT_ROOT"
echo "[dev] Serving book on http://localhost:8000"
echo "[dev] Open http://localhost:8000/book.html in browser"
echo "[dev] Ctrl+C to stop"

python -m http.server 8000
