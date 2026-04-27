#!/usr/bin/env bash
# scripts/dev-up.sh — bring up the dev stack with hot reload.
#
# Foreground by default so logs from both services stream to the terminal.
# Pass `-d` for detached.
#
# Hot-reload sources:
#   API:    src/ranker_service/, src/ranker/  (uvicorn --reload)
#   Webapp: webapp/src/, webapp/index.html    (Vite HMR)
#
# Usage:
#   scripts/dev-up.sh             # foreground, --build
#   scripts/dev-up.sh -d          # detached
#
# Reach the stack at:
#   webapp → http://localhost:5173
#   api    → http://localhost:8000

set -euo pipefail
cd "$(dirname "$0")/.."

echo "[dev-up] webapp → http://localhost:5173"
echo "[dev-up] api    → http://localhost:8000"
echo

exec docker compose up --build "$@"
