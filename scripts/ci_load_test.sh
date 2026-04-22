#!/usr/bin/env bash
# ci_load_test.sh — CI gate: spin up Docker stack, run step-load test, tear down.
#
# Exit codes:
#   0  All SLA assertions passed
#   1  SLA breach OR stack failed to start
#
# Usage:
#   bash scripts/ci_load_test.sh
#   bash scripts/ci_load_test.sh --api http://staging.host:8000  # skip docker steps
#
# Environment variables:
#   LOAD_TEST_API      Override API base URL (skips docker compose up/down)
#   LOAD_TEST_INPUT    Label file for image pool (default: data/eval/dev_set.json)
#   LOAD_TEST_PHASES   JSON phases override
#   READYZ_TIMEOUT     Seconds to wait for /readyz (default 120)
#   SKIP_DOCKER        Set to "1" to skip compose up/down (use existing stack)
#
# Example CI step (GitHub Actions):
#   - name: Load test
#     run: bash scripts/ci_load_test.sh
#     env:
#       LOAD_TEST_INPUT: data/eval/dev_set.json

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────

API="${LOAD_TEST_API:-http://localhost:8000}"
INPUT="${LOAD_TEST_INPUT:-data/eval/dev_set.json}"
READYZ_TIMEOUT="${READYZ_TIMEOUT:-120}"
SKIP_DOCKER="${SKIP_DOCKER:-0}"
REPORT_DIR="load_reports"

# ── Colour helpers ────────────────────────────────────────────────────────────

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[ci_load_test]${NC} $*"; }
warn()  { echo -e "${YELLOW}[ci_load_test]${NC} $*"; }
error() { echo -e "${RED}[ci_load_test]${NC} $*" >&2; }

# ── Parse args ────────────────────────────────────────────────────────────────

for arg in "$@"; do
  case $arg in
    --api=*)   API="${arg#*=}" ;;
    --api)     shift; API="$1" ;;
    --input=*) INPUT="${arg#*=}" ;;
    --skip-docker) SKIP_DOCKER=1 ;;
  esac
done

# ── Cleanup trap ──────────────────────────────────────────────────────────────

DOCKER_STARTED=0

cleanup() {
  if [[ "$DOCKER_STARTED" == "1" && "$SKIP_DOCKER" == "0" ]]; then
    info "Tearing down Docker stack …"
    docker compose down --remove-orphans || true
  fi
}
trap cleanup EXIT

# ── Step 1: Start Docker stack ────────────────────────────────────────────────

if [[ "$SKIP_DOCKER" == "0" ]]; then
  info "Starting Docker stack …"
  docker compose up -d
  DOCKER_STARTED=1
else
  info "SKIP_DOCKER=1 — assuming stack already running at $API"
fi

# ── Step 2: Wait for /readyz ──────────────────────────────────────────────────

info "Waiting for ${API}/readyz (timeout ${READYZ_TIMEOUT}s) …"
deadline=$(( $(date +%s) + READYZ_TIMEOUT ))
until curl -sf "${API}/readyz" > /dev/null 2>&1; do
  if [[ $(date +%s) -ge $deadline ]]; then
    error "Timed out waiting for /readyz after ${READYZ_TIMEOUT}s"
    if [[ "$SKIP_DOCKER" == "0" ]]; then
      warn "Worker logs:"
      docker compose logs --tail=40 worker || true
    fi
    exit 1
  fi
  sleep 2
done
info "/readyz OK"

# ── Step 3: Ensure dev set exists ─────────────────────────────────────────────

if [[ ! -f "$INPUT" ]]; then
  warn "Input file not found: $INPUT"
  warn "Creating dev/test split now …"
  python scripts/split_eval_set.py
fi

# ── Step 4: Run load test ─────────────────────────────────────────────────────

info "Running load test  api=$API  input=$INPUT"
mkdir -p "$REPORT_DIR"

LOAD_ARGS=(
  --api     "$API"
  --input   "$INPUT"
  --report  "$REPORT_DIR"
)

if [[ -n "${LOAD_TEST_PHASES:-}" ]]; then
  LOAD_ARGS+=(--phases "$LOAD_TEST_PHASES")
fi

# run_experiment.py exit code propagates: 0 = pass, 1 = SLA breach
set +e
python scripts/load_test.py "${LOAD_ARGS[@]}"
LOAD_EXIT=$?
set -e

# ── Step 5: Report outcome ────────────────────────────────────────────────────

if [[ $LOAD_EXIT -eq 0 ]]; then
  info "✓ Load test PASSED — all SLA assertions met"
else
  error "✗ Load test FAILED — SLA breach (exit $LOAD_EXIT)"
fi

exit $LOAD_EXIT
