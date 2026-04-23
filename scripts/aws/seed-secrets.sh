#!/usr/bin/env bash
# ============================================================================
# seed-secrets.sh — push runtime secrets into AWS SSM Parameter Store
# ============================================================================
#
# Reads a local .env.${ENV} file and writes every non-comment KEY=value line
# as a SecureString parameter at /invoice-ocr/${ENV}/${KEY}.
#
# This is the one-time bootstrap.  The deploy workflow and the on-box
# pull-secrets.sh both read from SSM — they never touch the local .env.
#
# Usage
# -----
#   # Create your .env.staging first (gitignored):
#   GEMINI_API_KEY=...
#   POSTGRES_PASSWORD=choose_a_strong_password
#   MINIO_ROOT_USER=ocr_admin
#   MINIO_ROOT_PASSWORD=choose_a_different_strong_password
#
#   ENV=staging bash scripts/aws/seed-secrets.sh
#
# Re-running is safe — --overwrite updates existing params in place.
# ============================================================================
set -euo pipefail

# Git Bash path-conversion safety (no-op on Linux/macOS)
export MSYS_NO_PATHCONV=1

: "${ENV:?must set ENV=staging|prod}"
case "$ENV" in staging|prod) ;; *) echo "ENV must be staging or prod" >&2; exit 1 ;; esac

export AWS_PROFILE="${AWS_PROFILE:-invoice-ocr}"
export AWS_REGION="${AWS_REGION:-us-east-1}"
export AWS_DEFAULT_REGION="$AWS_REGION"

AWS="$(command -v aws 2>/dev/null || echo "/c/Program Files/Amazon/AWSCLIV2/aws.exe")"

ENVFILE=".env.${ENV}"
if [[ ! -f "$ENVFILE" ]]; then
    cat >&2 <<ERR
ERROR: $ENVFILE not found.

Create it with at least:
  GEMINI_API_KEY=...
  POSTGRES_PASSWORD=...
  MINIO_ROOT_USER=...
  MINIO_ROOT_PASSWORD=...

It is gitignored by .gitignore (pattern "*.env.local" + explicit ".env*").
ERR
    exit 1
fi

# Required keys we absolutely MUST have before starting the stack
REQUIRED=(GEMINI_API_KEY POSTGRES_PASSWORD MINIO_ROOT_USER MINIO_ROOT_PASSWORD)
missing=()
for key in "${REQUIRED[@]}"; do
    grep -q "^$key=" "$ENVFILE" || missing+=("$key")
done
if (( ${#missing[@]} > 0 )); then
    echo "ERROR: $ENVFILE is missing required keys: ${missing[*]}" >&2
    exit 1
fi

count=0
while IFS= read -r line; do
    [[ -z "$line" || "${line:0:1}" == "#" ]] && continue
    key="${line%%=*}"
    val="${line#*=}"
    # Trim surrounding quotes if the user wrote KEY="value"
    val="${val#\"}"; val="${val%\"}"
    [[ -z "$key" || -z "$val" ]] && continue

    "$AWS" ssm put-parameter \
        --name "/invoice-ocr/${ENV}/${key}" \
        --type SecureString \
        --overwrite \
        --value "$val" >/dev/null
    printf "  ✓ /invoice-ocr/%s/%s\n" "$ENV" "$key"
    count=$((count+1))
done < "$ENVFILE"

echo
echo "Seeded $count SSM SecureString parameter(s) for ENV=$ENV"
