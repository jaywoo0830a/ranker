#!/usr/bin/env bash
# scripts/prod-up.sh — bring up the prod stack (nginx + API on single port).
#
# Detached by default — prod is long-running. Inspect logs with:
#   docker compose -f docker-compose.yml -f docker-compose.prod.yml logs -f
#
# Differences from dev:
#   - Webapp built once and served by nginx (no Vite, no HMR).
#   - Single public port (80) — nginx reverse-proxies /api/* to the api container.
#   - API has no source mount and no --reload.
#
# Usage:
#   scripts/prod-up.sh            # detached, --build
#   scripts/prod-up.sh --no-deps  # extra flags pass through

set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE_FILES=(-f docker-compose.yml -f docker-compose.prod.yml)

docker compose "${COMPOSE_FILES[@]}" up -d --build "$@"

echo
echo "[prod-up] → http://localhost"
echo "[prod-up] logs: docker compose ${COMPOSE_FILES[*]} logs -f"
