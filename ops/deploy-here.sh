#!/usr/bin/env bash
# ops/deploy-here.sh — pull an image SHA and reload the stack
#
# Invoked by the GitHub Actions deploy workflow via SSH.  Also usable by
# hand when operating on the box.
#
# Usage:
#   IMAGE=ghcr.io/CongNguyenGitHub/invoice-ocr:sha-abc1234 ENV=staging \
#       bash /opt/invoice-ocr/ops/deploy-here.sh
#
# Behavior:
#   1. Save the current IMAGE tag to .previous_sha (for rollback.sh)
#   2. Pull the new image
#   3. Rewrite .env with the new IMAGE= line
#   4. `docker compose up -d --remove-orphans`
#   5. Poll /readyz for up to 3 min
#   6. On failure: exit 1 (workflow decides whether to rollback)
set -euo pipefail

: "${ENV:?must set ENV}"
: "${IMAGE:?must set IMAGE=ghcr.io/...:sha-xxx}"
APP_DIR="${APP_DIR:-/opt/invoice-ocr}"
cd "$APP_DIR"

log() { echo "[deploy/$ENV] $*" >&2; }

# 1. Snapshot previous IMAGE for rollback
if [[ -f .env ]] && grep -q '^IMAGE=' .env; then
    grep '^IMAGE=' .env | head -1 | cut -d= -f2- > .previous_sha
    log "saved previous image → .previous_sha ($(< .previous_sha))"
fi

# 2. Write the new IMAGE line in .env (after refreshing secrets from SSM)
IMAGE="$IMAGE" ENV="$ENV" bash "$APP_DIR/ops/pull-secrets.sh"

# 3. Pull
log "pulling $IMAGE"
docker compose pull init api worker

# 4. Up
log "starting compose"
docker compose up -d --remove-orphans

# 5. Wait for /readyz
log "waiting for /readyz"
for i in $(seq 1 36); do
    if curl -sf http://localhost:8000/readyz >/dev/null; then
        log "✓ /readyz green after ${i}×5 s"
        exit 0
    fi
    sleep 5
done

log "ERROR: /readyz did not come green within 3 min"
log "recent api logs:"
docker compose logs --tail 50 api >&2 || true
exit 1
