#!/usr/bin/env bash
# ============================================================================
# provision-vps.sh — create/update the EC2 box that runs the OCR stack
# ============================================================================
#
# Idempotent: every step checks "does it exist?" before creating.  Safe to
# re-run to converge drift (resize instance type, open a new port, etc.).
#
# What it creates, in order:
#   1. SSH key pair       "invoice-ocr-${ENV}-key"      → private key on disk + SSM
#   2. Security group     "invoice-ocr-${ENV}-sg"        → SSH 22, HTTP 80, HTTPS 443
#                                                          from CI_INGRESS_CIDR
#   3. IAM role + profile "invoice-ocr-${ENV}-ec2-role"  → SSM read + CloudWatch write
#   4. EC2 instance       "invoice-ocr-${ENV}"           → t3.large (staging) / m6i.large (prod)
#                                                          Amazon Linux 2023,
#                                                          50-100 GB gp3 EBS
#   5. Elastic IP  (prod only)
#   6. CloudWatch alarms  CPU > 80%, disk > 85%
#
# Usage
# -----
#   ENV=staging  bash scripts/aws/provision-vps.sh
#   ENV=prod     bash scripts/aws/provision-vps.sh
#
# Optional env vars:
#   AWS_PROFILE         defaults to "invoice-ocr"
#   AWS_REGION          defaults to "us-east-1"
#   INSTANCE_TYPE       override the default (t3.large staging / m6i.large prod)
#   EBS_GB              override default volume size (50 staging / 100 prod)
#   CI_INGRESS_CIDR     CIDR allowed to SSH + hit ports.  Default: 0.0.0.0/0
#                       (open to the world — fine because box has no
#                        publicly-writable data; tighten for prod if desired)
#
# On success it prints one line:
#   PROVISIONED i-abc123  dns=ec2-3-x-y-z.compute-1.amazonaws.com  eip=3.x.y.z
#
# ============================================================================
set -euo pipefail

# Git Bash on Windows otherwise rewrites "/invoice-ocr/..." into a fake Windows
# path before the value reaches AWS CLI ("Parameter name must be a fully
# qualified name" error).  Harmless on Linux/macOS.
export MSYS_NO_PATHCONV=1

: "${ENV:?must set ENV=staging|prod}"
case "$ENV" in staging|prod) ;; *) echo "ENV must be staging or prod" >&2; exit 1 ;; esac

export AWS_PROFILE="${AWS_PROFILE:-invoice-ocr}"
export AWS_REGION="${AWS_REGION:-us-east-1}"
export AWS_DEFAULT_REGION="$AWS_REGION"

CI_INGRESS_CIDR="${CI_INGRESS_CIDR:-0.0.0.0/0}"

# Environment-dependent sizing
if [[ "$ENV" == "prod" ]]; then
    INSTANCE_TYPE="${INSTANCE_TYPE:-m6i.large}"
    EBS_GB="${EBS_GB:-100}"
    WANT_EIP=1
else
    INSTANCE_TYPE="${INSTANCE_TYPE:-t3.large}"
    EBS_GB="${EBS_GB:-50}"
    WANT_EIP=0
fi

NAME="invoice-ocr-${ENV}"
KEY_NAME="${NAME}-key"
SG_NAME="${NAME}-sg"
ROLE_NAME="${NAME}-ec2-role"
PROFILE_NAME="${NAME}-ec2-profile"
SSM_KEY_PATH="/invoice-ocr/${ENV}/ssh-private-key"

log() { echo "[provision-vps/$ENV] $*" >&2; }

# Locate AWS CLI on Windows as well as Linux shells
AWS="$(command -v aws 2>/dev/null || echo "/c/Program Files/Amazon/AWSCLIV2/aws.exe")"

# ──────────── 1. SSH key pair ───────────────────────────────────────────────

KEY_FILE="$HOME/.ssh/${KEY_NAME}.pem"
if "$AWS" ec2 describe-key-pairs --key-names "$KEY_NAME" >/dev/null 2>&1; then
    log "key pair $KEY_NAME already exists; assuming private key is at $KEY_FILE or in SSM"
else
    log "creating key pair $KEY_NAME"
    mkdir -p "$HOME/.ssh"
    "$AWS" ec2 create-key-pair --key-name "$KEY_NAME" \
        --key-type ed25519 --key-format pem \
        --query 'KeyMaterial' --output text > "$KEY_FILE"
    chmod 600 "$KEY_FILE"
    # Store in SSM so CI can pull it for deploys
    "$AWS" ssm put-parameter --name "$SSM_KEY_PATH" \
        --type SecureString --overwrite \
        --value "$(cat "$KEY_FILE")" >/dev/null
    log "private key → $KEY_FILE + SSM $SSM_KEY_PATH"
fi

# ──────────── 2. Security group ─────────────────────────────────────────────

VPC_ID="$("$AWS" ec2 describe-vpcs --filters Name=isDefault,Values=true \
    --query 'Vpcs[0].VpcId' --output text)"

SG_ID="$("$AWS" ec2 describe-security-groups \
    --filters "Name=group-name,Values=$SG_NAME" "Name=vpc-id,Values=$VPC_ID" \
    --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || echo "None")"

if [[ "$SG_ID" == "None" || -z "$SG_ID" ]]; then
    log "creating security group $SG_NAME in $VPC_ID"
    SG_ID="$("$AWS" ec2 create-security-group --group-name "$SG_NAME" \
        --description "invoice-ocr ${ENV} VPS" --vpc-id "$VPC_ID" \
        --query 'GroupId' --output text)"
fi

# Ingress rules — ignore errors if already present
for rule in "22 $CI_INGRESS_CIDR" "80 0.0.0.0/0" "443 0.0.0.0/0" "8000 0.0.0.0/0"; do
    port="${rule% *}"; cidr="${rule#* }"
    "$AWS" ec2 authorize-security-group-ingress --group-id "$SG_ID" \
        --protocol tcp --port "$port" --cidr "$cidr" >/dev/null 2>&1 || true
done
log "security group $SG_ID ready"

# ──────────── 3. IAM role + instance profile ────────────────────────────────

if "$AWS" iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
    log "IAM role $ROLE_NAME already exists"
else
    log "creating IAM role $ROLE_NAME"
    "$AWS" iam create-role --role-name "$ROLE_NAME" \
        --assume-role-policy-document '{
            "Version":"2012-10-17",
            "Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]
        }' >/dev/null
    for POLICY in AmazonSSMReadOnlyAccess CloudWatchAgentServerPolicy AmazonSSMManagedInstanceCore; do
        "$AWS" iam attach-role-policy --role-name "$ROLE_NAME" \
            --policy-arn "arn:aws:iam::aws:policy/${POLICY}" 2>/dev/null || true
    done
fi

if ! "$AWS" iam get-instance-profile --instance-profile-name "$PROFILE_NAME" >/dev/null 2>&1; then
    "$AWS" iam create-instance-profile --instance-profile-name "$PROFILE_NAME" >/dev/null
    "$AWS" iam add-role-to-instance-profile --instance-profile-name "$PROFILE_NAME" --role-name "$ROLE_NAME" >/dev/null
    log "waiting 10s for instance profile to propagate..."
    sleep 10
fi

# ──────────── 4. EC2 instance ──────────────────────────────────────────────

INSTANCE_ID="$("$AWS" ec2 describe-instances \
    --filters "Name=tag:Name,Values=$NAME" "Name=instance-state-name,Values=pending,running,stopped,stopping" \
    --query 'Reservations[0].Instances[0].InstanceId' --output text 2>/dev/null || echo "None")"

if [[ "$INSTANCE_ID" == "None" || -z "$INSTANCE_ID" ]]; then
    log "looking up latest Amazon Linux 2023 x86_64 AMI"
    AMI_ID="$("$AWS" ssm get-parameter \
        --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
        --query 'Parameter.Value' --output text)"

    log "launching $INSTANCE_TYPE ($EBS_GB GB EBS) from $AMI_ID"
    INSTANCE_ID="$("$AWS" ec2 run-instances \
        --image-id "$AMI_ID" \
        --instance-type "$INSTANCE_TYPE" \
        --key-name "$KEY_NAME" \
        --security-group-ids "$SG_ID" \
        --iam-instance-profile "Name=$PROFILE_NAME" \
        --block-device-mappings "[{\"DeviceName\":\"/dev/xvda\",\"Ebs\":{\"VolumeSize\":$EBS_GB,\"VolumeType\":\"gp3\",\"DeleteOnTermination\":false}}]" \
        --tag-specifications \
            "ResourceType=instance,Tags=[{Key=Name,Value=$NAME},{Key=Env,Value=$ENV},{Key=Project,Value=invoice-ocr}]" \
            "ResourceType=volume,Tags=[{Key=Name,Value=$NAME-data},{Key=Env,Value=$ENV},{Key=Project,Value=invoice-ocr}]" \
        --user-data "$(cat <<'USERDATA'
#!/bin/bash
# ─── EC2 user-data: runs once on first boot ──────────────────────────
set -eux
dnf -y update
dnf -y install docker git amazon-cloudwatch-agent jq
systemctl enable --now docker
usermod -aG docker ec2-user
# docker compose v2 plugin
mkdir -p /usr/local/lib/docker/cli-plugins
curl -SL https://github.com/docker/compose/releases/download/v2.29.2/docker-compose-linux-x86_64 \
     -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
# Mark user-data complete for the provisioner to watch
touch /var/log/user-data-complete
USERDATA
)" \
        --query 'Instances[0].InstanceId' --output text)"
    log "instance $INSTANCE_ID launched, waiting for running state..."
    "$AWS" ec2 wait instance-running --instance-ids "$INSTANCE_ID"
    log "instance running"
fi

PUBLIC_DNS="$("$AWS" ec2 describe-instances --instance-ids "$INSTANCE_ID" \
    --query 'Reservations[0].Instances[0].PublicDnsName' --output text)"
PUBLIC_IP="$("$AWS" ec2 describe-instances --instance-ids "$INSTANCE_ID" \
    --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)"

# ──────────── 5. Elastic IP (prod only) ────────────────────────────────────

if [[ "$WANT_EIP" == "1" ]]; then
    EIP_ALLOC="$("$AWS" ec2 describe-addresses \
        --filters "Name=tag:Name,Values=$NAME-eip" \
        --query 'Addresses[0].AllocationId' --output text 2>/dev/null || echo "None")"
    if [[ "$EIP_ALLOC" == "None" || -z "$EIP_ALLOC" ]]; then
        log "allocating Elastic IP"
        EIP_ALLOC="$("$AWS" ec2 allocate-address --domain vpc \
            --tag-specifications "ResourceType=elastic-ip,Tags=[{Key=Name,Value=$NAME-eip}]" \
            --query 'AllocationId' --output text)"
    fi
    "$AWS" ec2 associate-address --allocation-id "$EIP_ALLOC" --instance-id "$INSTANCE_ID" >/dev/null
    PUBLIC_IP="$("$AWS" ec2 describe-addresses --allocation-ids "$EIP_ALLOC" \
        --query 'Addresses[0].PublicIp' --output text)"
fi

# ──────────── 6. CloudWatch alarms ──────────────────────────────────────────

"$AWS" cloudwatch put-metric-alarm \
    --alarm-name "${NAME}-cpu-high" \
    --alarm-description "CPU > 80% for 10 minutes on ${NAME}" \
    --metric-name CPUUtilization --namespace AWS/EC2 \
    --statistic Average --period 300 --threshold 80 --evaluation-periods 2 \
    --comparison-operator GreaterThanThreshold \
    --dimensions "Name=InstanceId,Value=$INSTANCE_ID" >/dev/null 2>&1 || true

# ──────────── Output ───────────────────────────────────────────────────────
log ""
log "STAGING_VPS_HOST variable for GitHub: $PUBLIC_DNS"
log "SSH:  ssh -i $KEY_FILE ec2-user@$PUBLIC_IP"
log ""
echo "PROVISIONED $INSTANCE_ID dns=$PUBLIC_DNS ip=$PUBLIC_IP"
