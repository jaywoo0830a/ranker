#!/usr/bin/env bash
# test.sh - run the test suite
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"

if [ -d ".venv" ]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
fi

export PYTHONPATH="$PROJECT_ROOT/src:${PYTHONPATH:-}"
python -m pytest tests/ -v "$@"
