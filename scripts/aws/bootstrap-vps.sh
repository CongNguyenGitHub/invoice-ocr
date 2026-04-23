#!/usr/bin/env bash
# ============================================================================
# bootstrap-vps.sh — runs ON the EC2 box to set up the OCR stack
# ============================================================================
#
# Idempotent.  Safe to re-run after a kernel update or to refresh the
# systemd unit / Makefile.
#
# What it does:
#   1. Ensures docker + docker compose plugin + git + awscli are present
#      (user-data should have done this; this script catches the case where
#      user-data hasn't completed yet or someone re-launched the box).
#   2. Clones / updates the repo at /opt/invoice-ocr
#   3. Installs ops/Makefile, ops/pull-secrets.sh, ops/rollback.sh, ops/snapshot.sh
#   4. Installs the systemd unit invoice-ocr.service
#   5. Installs cron entries for hourly EBS snapshot
#   6. Pulls the initial set of secrets from SSM into /opt/invoice-ocr/.env
#   7. Starts the stack (`systemctl enable --now invoice-ocr`)
#
# Usage (from the operator's laptop, after provision-vps.sh)
# ----------------------------------------------------------
#   scp -i ~/.ssh/invoice-ocr-staging-key.pem \
#       scripts/aws/bootstrap-vps.sh \
#       ec2-user@<public-dns>:/tmp/bootstrap-vps.sh
#   ssh -i ~/.ssh/invoice-ocr-staging-key.pem \
#       ec2-user@<public-dns> \
#       'sudo ENV=staging bash /tmp/bootstrap-vps.sh'
#
# Required env on invocation:
#   ENV=staging|prod
#   REPO_URL (default: https://github.com/CongNguyenGitHub/invoice-ocr.git)
#   GIT_REF  (default: main)
# ============================================================================
set -euo pipefail

: "${ENV:?must set ENV=staging|prod}"
# Default to SSH URL — the box is expected to hold a read-only deploy key at
# ~ec2-user/.ssh/id_ed25519_deploy configured via ~/.ssh/config.  Override to
# the https URL if you've made the repo public.
REPO_URL="${REPO_URL:-git@github.com:CongNguyenGitHub/invoice-ocr.git}"
GIT_REF="${GIT_REF:-main}"
APP_DIR="/opt/invoice-ocr"

log() { echo "[bootstrap/$ENV] $*" >&2; }

# ──────────── 1. Tooling ───────────────────────────────────────────────────

# Wait until user-data has finished installing docker (max 5 min)
for _ in $(seq 1 60); do
    [[ -f /var/log/user-data-complete ]] && break
    sleep 5
done

# Belt & braces: install anything user-data may have skipped
dnf -y install docker git jq amazon-cloudwatch-agent unzip 2>/dev/null || true
systemctl enable --now docker

# AWS CLI v2 (the dnf "awscli" package is v1)
if ! command -v aws >/dev/null 2>&1; then
    log "installing AWS CLI v2"
    cd /tmp
    curl -sL https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip -o awscliv2.zip
    unzip -q awscliv2.zip
    ./aws/install
    rm -rf aws awscliv2.zip
fi

# docker compose plugin
if ! docker compose version >/dev/null 2>&1; then
    mkdir -p /usr/local/lib/docker/cli-plugins
    curl -sL https://github.com/docker/compose/releases/download/v2.29.2/docker-compose-linux-x86_64 \
        -o /usr/local/lib/docker/cli-plugins/docker-compose
    chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
fi

usermod -aG docker ec2-user 2>/dev/null || true

# ──────────── 2. Repo ──────────────────────────────────────────────────────

# /opt is root-owned; we want ec2-user to own $APP_DIR so the deploy SSH key
# (~ec2-user/.ssh/id_ed25519_deploy) is used for git fetches.
# Strategy: if the repo is missing, clone fresh as ec2-user into a tmp path
# under their home (no perms issue), then move it into place as root.
mkdir -p "$(dirname "$APP_DIR")"
if [[ ! -d "$APP_DIR/.git" ]]; then
    log "cloning $REPO_URL → $APP_DIR (as ec2-user)"
    rm -rf "$APP_DIR" /home/ec2-user/_clone_tmp
    sudo -u ec2-user -H git clone "$REPO_URL" /home/ec2-user/_clone_tmp
    mv /home/ec2-user/_clone_tmp "$APP_DIR"
else
    log "updating $APP_DIR"
    sudo -u ec2-user -H git -C "$APP_DIR" fetch --depth 1 origin "$GIT_REF"
fi
chown -R ec2-user:ec2-user "$APP_DIR"
sudo -u ec2-user -H git -C "$APP_DIR" checkout "$GIT_REF"
sudo -u ec2-user -H git -C "$APP_DIR" reset --hard "origin/${GIT_REF}"

# ──────────── 3. ops/ scripts go to /opt/invoice-ocr ───────────────────────

# Already present in repo at $APP_DIR/ops/.  Make them executable.
chmod +x "$APP_DIR/ops/"*.sh 2>/dev/null || true

# Symlink the Makefile to /opt/invoice-ocr/Makefile for easy `cd /opt/invoice-ocr && make logs`
ln -sf "$APP_DIR/ops/Makefile" "$APP_DIR/Makefile"

# ──────────── 4. systemd unit ──────────────────────────────────────────────

cp "$APP_DIR/ops/systemd/invoice-ocr.service" /etc/systemd/system/invoice-ocr.service
# Inject ENV into the unit's Environment= line
sed -i "s|__ENV__|$ENV|g" /etc/systemd/system/invoice-ocr.service
systemctl daemon-reload
systemctl enable invoice-ocr.service

# ──────────── 5. cron — hourly snapshot ────────────────────────────────────

# Amazon Linux 2023 ships systemd-only by default; install cronie if absent
# so /etc/cron.d works.
if [[ ! -d /etc/cron.d ]]; then
    log "installing cronie"
    dnf -y install cronie
    systemctl enable --now crond
fi

# Use a single line in /etc/cron.d so re-running is idempotent
cat > /etc/cron.d/invoice-ocr-snapshot <<EOF
# hourly EBS snapshot of the data volume
17 * * * * root ENV=$ENV /opt/invoice-ocr/ops/snapshot.sh >> /var/log/invoice-ocr-snapshot.log 2>&1
EOF
chmod 0644 /etc/cron.d/invoice-ocr-snapshot

# ──────────── 6. Pull initial secrets ──────────────────────────────────────

log "pulling secrets from SSM"
ENV=$ENV "$APP_DIR/ops/pull-secrets.sh"

# ──────────── 7. Start the stack ───────────────────────────────────────────

log "starting docker compose via systemd"
systemctl restart invoice-ocr.service

# Smoke check — wait up to 3 min for /readyz
log "waiting for /readyz to come green..."
for i in $(seq 1 36); do
    if curl -sf http://localhost:8000/readyz >/dev/null; then
        log "✓ /readyz green after ${i}×5 s"
        break
    fi
    sleep 5
done
curl -sf http://localhost:8000/readyz | head -1 || {
    log "WARNING: /readyz not green within 3 min — check 'docker compose logs'"
    exit 1
}

log "DONE — stack is up"
