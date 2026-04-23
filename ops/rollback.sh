#!/usr/bin/env bash
# ops/rollback.sh — revert to the previous deployed image SHA
#
# The deploy workflow writes the previous IMAGE tag to .previous_sha BEFORE
# starting the new deploy.  Rolling back is just: read that file, set IMAGE,
# `docker compose up -d`, wait for /readyz.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/invoice-ocr}"
cd "$APP_DIR"

if [[ ! -s .previous_sha ]]; then
    echo "ERROR: $APP_DIR/.previous_sha is missing or empty — nothing to roll back to." >&2
    exit 1
fi

PREV="$(< .previous_sha)"
echo "rolling back to $PREV"

# Update .env IMAGE line in place
if grep -q '^IMAGE=' .env; then
    sed -i "s|^IMAGE=.*|IMAGE=$PREV|" .env
else
    echo "IMAGE=$PREV" >> .env
fi

docker compose pull
docker compose up -d --remove-orphans

# Wait /readyz up to 2 min
for i in $(seq 1 24); do
    if curl -sf http://localhost:8000/readyz >/dev/null; then
        echo "✓ /readyz green after ${i}×5 s"
        exit 0
    fi
    sleep 5
done
echo "ERROR: /readyz did not come green after rollback" >&2
exit 1
