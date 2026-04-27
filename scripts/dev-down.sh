#!/usr/bin/env bash
# scripts/dev-down.sh — stop and remove the dev stack containers.
#
# Job artifacts (the `jobs_data` named volume) are PRESERVED by default —
# results from earlier runs survive across restarts. To wipe everything:
#   scripts/dev-down.sh -v
#
# Usage:
#   scripts/dev-down.sh           # stop + remove containers, keep volumes
#   scripts/dev-down.sh -v        # also remove volumes (destroys job history)
#   scripts/dev-down.sh --rmi all # also remove images

set -euo pipefail
cd "$(dirname "$0")/.."

exec docker compose down "$@"
