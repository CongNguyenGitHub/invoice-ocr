#!/usr/bin/env bash
# ============================================================================
# upload-models.sh — push YOLOv11n weights + config to S3
# ============================================================================
#
# Run this from your laptop whenever the model.onnx file changes.  The EC2
# instance role has read-only access to this bucket (attached by
# provision-vps.sh), so deploy-here.sh / bootstrap-vps.sh on the box can
# `aws s3 sync` it down.
#
# The bucket is versioned, so uploading a new weight does NOT destroy the
# old one — you can always roll back by downloading an older version.
#
# Usage
# -----
#   bash scripts/aws/upload-models.sh
# ============================================================================
set -euo pipefail

export MSYS_NO_PATHCONV=1
export AWS_PROFILE="${AWS_PROFILE:-invoice-ocr}"
export AWS_REGION="${AWS_REGION:-us-east-1}"
export AWS_DEFAULT_REGION="$AWS_REGION"

AWS="$(command -v aws 2>/dev/null || echo "/c/Program Files/Amazon/AWSCLIV2/aws.exe")"

ACCOUNT_ID="$("$AWS" sts get-caller-identity --query Account --output text)"
BUCKET="${MODELS_BUCKET:-invoice-ocr-models-${ACCOUNT_ID}}"

# Create bucket if missing (idempotent)
if ! "$AWS" s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
    echo "[upload-models] creating bucket $BUCKET"
    "$AWS" s3api create-bucket --bucket "$BUCKET" --region "$AWS_REGION" >/dev/null
    "$AWS" s3api put-public-access-block --bucket "$BUCKET" \
        --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
    "$AWS" s3api put-bucket-versioning --bucket "$BUCKET" \
        --versioning-configuration Status=Enabled
fi

echo "[upload-models] syncing models/ → s3://${BUCKET}/"
"$AWS" s3 sync models/ "s3://${BUCKET}/" --exclude '*' --include 'yolov11n_receipt/*' --include 'yolov11n_receipt/**'

echo "[upload-models] contents:"
"$AWS" s3 ls "s3://${BUCKET}/" --recursive --human-readable
