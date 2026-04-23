#!/usr/bin/env bash
# init.sh - run once to set up the environment
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

echo "[init] Project root: $PROJECT_ROOT"
echo "[init] Creating virtual environment..."
cd "$PROJECT_ROOT"

python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate

echo "[init] Upgrading pip..."
pip install --upgrade pip

echo "[init] Installing dependencies..."
pip install pytest

echo "[init] Done."
echo "[init] Next: source .venv/bin/activate && ./run/test.sh"
