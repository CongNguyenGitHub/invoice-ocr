#!/usr/bin/env bash
# ops/snapshot.sh — hourly EBS snapshot of the data volume
#
# Discovers the EC2 instance ID via IMDSv2, finds the data EBS volume by tag,
# then creates a snapshot tagged with the timestamp.  An AWS Data Lifecycle
# Manager (DLM) policy can prune old snapshots; for early stage we just keep
# the last 168 (= one week of hourly).
set -euo pipefail

: "${ENV:?must set ENV=staging|prod}"
TAG_PREFIX="invoice-ocr-${ENV}"
KEEP=168   # last 7 days × 24 hours

# IMDSv2: get a token, then ask for the instance ID
TOKEN="$(curl -sX PUT 'http://169.254.169.254/latest/api/token' \
    -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')"
INSTANCE_ID="$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \
    http://169.254.169.254/latest/meta-data/instance-id)"
REGION="$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \
    http://169.254.169.254/latest/meta-data/placement/region)"
export AWS_DEFAULT_REGION="$REGION"

# The data volume = the only volume on this instance with our project tag
VOL_ID="$(aws ec2 describe-volumes \
    --filters "Name=attachment.instance-id,Values=$INSTANCE_ID" \
              "Name=tag:Project,Values=invoice-ocr" \
    --query 'Volumes[0].VolumeId' --output text)"

if [[ -z "$VOL_ID" || "$VOL_ID" == "None" ]]; then
    echo "ERROR: no project-tagged volume on $INSTANCE_ID" >&2
    exit 1
fi

TS="$(date -u +%Y%m%d-%H%M)"
DESC="$TAG_PREFIX hourly snapshot $TS"

aws ec2 create-snapshot \
    --volume-id "$VOL_ID" \
    --description "$DESC" \
    --tag-specifications "ResourceType=snapshot,Tags=[{Key=Project,Value=invoice-ocr},{Key=Env,Value=$ENV},{Key=AutoSnapshot,Value=true},{Key=Name,Value=${TAG_PREFIX}-${TS}}]" \
    --query 'SnapshotId' --output text \
    | xargs -I {} echo "created {} of $VOL_ID"

# Prune oldest if we exceed KEEP
SNAP_IDS="$(aws ec2 describe-snapshots --owner-ids self \
    --filters "Name=tag:Project,Values=invoice-ocr" \
              "Name=tag:Env,Values=$ENV" \
              "Name=tag:AutoSnapshot,Values=true" \
    --query 'Snapshots | sort_by(@, &StartTime) | [].SnapshotId' --output text)"
COUNT="$(echo "$SNAP_IDS" | wc -w)"
if (( COUNT > KEEP )); then
    DELETE_COUNT=$(( COUNT - KEEP ))
    echo "pruning $DELETE_COUNT old snapshot(s)"
    echo "$SNAP_IDS" | tr ' ' '\n' | head -n "$DELETE_COUNT" | while read -r SID; do
        aws ec2 delete-snapshot --snapshot-id "$SID" && echo "  deleted $SID"
    done
fi
