#!/usr/bin/env bash
# ops/pull-secrets.sh — fetch /invoice-ocr/${ENV}/* from SSM, write .env
#
# Runs on the EC2 box.  Uses the instance's IAM role (no creds needed).
# Called once on first bootstrap, and before every `docker compose up` in deploy.
# Re-running is safe: overwrites .env in place.
set -euo pipefail

: "${ENV:?must set ENV=staging|prod}"
APP_DIR="${APP_DIR:-/opt/invoice-ocr}"
ENVFILE="${APP_DIR}/.env"
PREFIX="/invoice-ocr/${ENV}/"

# get-parameters-by-path paginates internally — we request one page at a time
tmp="$(mktemp)"
next=""
while :; do
    if [[ -z "$next" ]]; then
        out="$(aws ssm get-parameters-by-path --path "$PREFIX" --recursive \
            --with-decryption --max-results 10 --output json)"
    else
        out="$(aws ssm get-parameters-by-path --path "$PREFIX" --recursive \
            --with-decryption --max-results 10 --starting-token "$next" --output json)"
    fi
    # Extract KEY=VALUE pairs, excluding non-runtime secrets (SSH key, GHCR PAT)
    echo "$out" | jq -r --arg prefix "$PREFIX" '
        .Parameters[]
        | select(.Name | endswith("ssh-private-key") | not)
        | select(.Name | endswith("ghcr-pull-pat") | not)
        | "\(.Name | sub($prefix; ""))=\(.Value)"
    ' >> "$tmp"
    next="$(echo "$out" | jq -r '.NextToken // empty')"
    [[ -z "$next" ]] && break
done

# ────────────────────── docker login ghcr.io ──────────────────────────────
# The image lives in a private GHCR package, so docker pull needs auth.
# Pull the read-only PAT from SSM and `docker login` ec2-user.  The login
# persists in /home/ec2-user/.docker/config.json — re-running is harmless.
PAT="$(aws ssm get-parameter --name "${PREFIX}ghcr-pull-pat" \
    --with-decryption --query 'Parameter.Value' --output text 2>/dev/null || true)"
if [[ -n "$PAT" && "$PAT" != "None" ]]; then
    echo "$PAT" | docker login ghcr.io -u CongNguyenGitHub --password-stdin >/dev/null
    echo "✓ docker logged in to ghcr.io"
else
    echo "WARNING: no ghcr-pull-pat in SSM — docker pull may fail for private images" >&2
fi

# Static, non-secret env vars the stack needs
cat >> "$tmp" <<EOF

# ── non-secret env (set by pull-secrets.sh) ──
ENV=$ENV
PROMPT_SEMANTIC_VERSION=${PROMPT_SEMANTIC_VERSION:-v3.7}
LOG_LEVEL=${LOG_LEVEL:-INFO}
WORKER_CONCURRENCY=${WORKER_CONCURRENCY:-4}
EOF

# If an IMAGE tag was passed (by the deploy workflow), persist it so
# `systemctl restart invoice-ocr` uses the pinned tag instead of :local
if [[ -n "${IMAGE:-}" ]]; then
    echo "IMAGE=$IMAGE" >> "$tmp"
fi

install -m 600 "$tmp" "$ENVFILE"
rm -f "$tmp"
echo "wrote $ENVFILE ($(wc -l < "$ENVFILE") lines)"
