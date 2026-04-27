#!/usr/bin/env bash
# scripts/prod-down.sh — stop and remove the prod stack containers.
#
# Job artifacts (the `jobs_data` named volume) are PRESERVED by default.
# To wipe the volume too:
#   scripts/prod-down.sh -v
#
# Usage:
#   scripts/prod-down.sh          # stop + remove containers, keep volumes
#   scripts/prod-down.sh -v       # also remove volumes (destroys job history)

set -euo pipefail
cd "$(dirname "$0")/.."

exec docker compose -f docker-compose.yml -f docker-compose.prod.yml down "$@"
