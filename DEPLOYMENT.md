# AWS EC2 Deployment Guide

Complete guide for deploying invoice-ocr on AWS EC2 with CPU-based inference.

## Overview

- **Instance Type**: t3.xlarge (4 vCPU, 16GB RAM, CPU-only)
- **Monthly Cost**: ~$143 (compute + storage + data transfer)
- **Container Registry**: AWS ECR
- **Secrets Management**: AWS SSM Parameter Store
- **CI/CD**: GitHub Actions
- **Deployment Strategy**: Simple rolling updates

---

## Prerequisites

- AWS Account with admin access
- GitHub repository
- Google Gemini API key
- Domain name (optional, for SSL)
- AWS CLI installed locally
- Docker installed locally

---

## Phase 1: AWS Infrastructure Setup (30 minutes)

### 1.1 Create ECR Repository

```bash
aws ecr create-repository \
    --repository-name invoice-ocr \
    --region us-east-1

# Note the repository URI
# Example: 123456789012.dkr.ecr.us-east-1.amazonaws.com/invoice-ocr
```

### 1.2 Create IAM Role for EC2

```bash
# Create trust policy
cat > /tmp/ec2-trust-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "ec2.amazonaws.com"},
    "Action": "sts:AssumeRole"
  }]
}
EOF

# Create role
aws iam create-role \
    --role-name invoice-ocr-ec2-role \
    --assume-role-policy-document file:///tmp/ec2-trust-policy.json

# Attach ECR read policy
aws iam attach-role-policy \
    --role-name invoice-ocr-ec2-role \
    --policy-arn arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly

# Attach CloudWatch logs policy
aws iam attach-role-policy \
    --role-name invoice-ocr-ec2-role \
    --policy-arn arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy

# Create SSM access policy
cat > /tmp/ssm-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": [
      "ssm:GetParameter",
      "ssm:GetParameters",
      "ssm:GetParametersByPath"
    ],
    "Resource": "arn:aws:ssm:*:*:parameter/invoice-ocr/*"
  }]
}
EOF

aws iam put-role-policy \
    --role-name invoice-ocr-ec2-role \
    --policy-name SSMParameterAccess \
    --policy-document file:///tmp/ssm-policy.json

# Create instance profile
aws iam create-instance-profile \
    --instance-profile-name invoice-ocr-ec2-profile

aws iam add-role-to-instance-profile \
    --instance-profile-name invoice-ocr-ec2-profile \
    --role-name invoice-ocr-ec2-role
```

### 1.3 Store Secrets in SSM Parameter Store

```bash
# Gemini API key
aws ssm put-parameter \
    --name /invoice-ocr/production/gemini-api-key \
    --value "YOUR_GEMINI_API_KEY" \
    --type SecureString

# PostgreSQL password
aws ssm put-parameter \
    --name /invoice-ocr/production/postgres-password \
    --value "$(openssl rand -base64 32)" \
    --type SecureString

# Allowed domains
aws ssm put-parameter \
    --name /invoice-ocr/production/allowed-domains \
    --value "img-campaign.gotit.vn" \
    --type String
```

### 1.4 Create Security Group

```bash
# Get default VPC
VPC_ID=$(aws ec2 describe-vpcs \
    --filters "Name=isDefault,Values=true" \
    --query "Vpcs[0].VpcId" \
    --output text)

# Create security group
SG_ID=$(aws ec2 create-security-group \
    --group-name invoice-ocr-ec2 \
    --description "Invoice OCR Security Group" \
    --vpc-id $VPC_ID \
    --output text --query 'GroupId')

# Allow SSH from your IP
aws ec2 authorize-security-group-ingress \
    --group-id $SG_ID \
    --protocol tcp \
    --port 22 \
    --cidr YOUR_IP/32

# Allow HTTP for API
aws ec2 authorize-security-group-ingress \
    --group-id $SG_ID \
    --protocol tcp \
    --port 8000 \
    --cidr 0.0.0.0/0

# Allow Grafana (restrict to your IP)
aws ec2 authorize-security-group-ingress \
    --group-id $SG_ID \
    --protocol tcp \
    --port 3000 \
    --cidr YOUR_IP/32
```

### 1.5 Launch EC2 Instance

```bash
# Create user data script
cat > /tmp/user-data.sh <<'EOF'
#!/bin/bash
set -e

# Update system
apt-get update
apt-get upgrade -y

# Install Docker
curl -fsSL https://get.docker.com -o get-docker.sh
sh get-docker.sh
usermod -aG docker ubuntu

# Install docker-compose
curl -L "https://github.com/docker/compose/releases/download/v2.20.0/docker-compose-$(uname -s)-$(uname -m)" \
    -o /usr/local/bin/docker-compose
chmod +x /usr/local/bin/docker-compose

# Install AWS CLI v2
cd /tmp
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"
unzip awscliv2.zip
./aws/install

# Create project directory
mkdir -p /opt/invoice-ocr
chown ubuntu:ubuntu /opt/invoice-ocr

echo "Instance setup complete"
EOF

# Launch instance
aws ec2 run-instances \
    --image-id ami-0c55b159cbfafe1f0 \
    --instance-type t3.xlarge \
    --key-name YOUR_KEY_NAME \
    --security-group-ids $SG_ID \
    --iam-instance-profile Name=invoice-ocr-ec2-profile \
    --user-data file:///tmp/user-data.sh \
    --block-device-mappings '[
        {"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":100,"VolumeType":"gp3"}}
    ]' \
    --tag-specifications 'ResourceType=instance,Tags=[
        {Key=Name,Value=invoice-ocr-production},
        {Key=Environment,Value=production}
    ]'

# Get instance ID
INSTANCE_ID=$(aws ec2 describe-instances \
    --filters "Name=tag:Name,Values=invoice-ocr-production" \
              "Name=instance-state-name,Values=running" \
    --query "Reservations[0].Instances[0].InstanceId" \
    --output text)

echo "Instance ID: $INSTANCE_ID"

# Wait for instance to be ready
aws ec2 wait instance-running --instance-ids $INSTANCE_ID

# Get public IP
INSTANCE_IP=$(aws ec2 describe-instances \
    --instance-ids $INSTANCE_ID \
    --query "Reservations[0].Instances[0].PublicIpAddress" \
    --output text)

echo "Instance IP: $INSTANCE_IP"
```

---

## Phase 2: EC2 Configuration (20 minutes)

### 2.1 SSH into Instance

```bash
ssh -i ~/.ssh/your-key.pem ubuntu@$INSTANCE_IP
```

### 2.2 Clone Repository

```bash
cd /opt/invoice-ocr
git clone https://github.com/YOUR_ORG/invoice-ocr.git .
```

### 2.3 Fetch Secrets from SSM

```bash
# Create simple secrets fetch script
cat > fetch-secrets.sh <<'EOF'
#!/bin/bash
set -e

GEMINI_API_KEY=$(aws ssm get-parameter \
    --name /invoice-ocr/production/gemini-api-key \
    --with-decryption \
    --query "Parameter.Value" \
    --output text)

POSTGRES_PASSWORD=$(aws ssm get-parameter \
    --name /invoice-ocr/production/postgres-password \
    --with-decryption \
    --query "Parameter.Value" \
    --output text)

ALLOWED_DOMAINS=$(aws ssm get-parameter \
    --name /invoice-ocr/production/allowed-domains \
    --query "Parameter.Value" \
    --output text)

# Create .env file
cat > .env <<EOE
GEMINI_API_KEY=$GEMINI_API_KEY
POSTGRES_PASSWORD=$POSTGRES_PASSWORD
ALLOWED_IMAGE_DOMAINS=$ALLOWED_DOMAINS
POSTGRES_DSN=postgresql+asyncpg://invoice:$POSTGRES_PASSWORD@postgres:5432/invoice_ocr
EOE

echo "✓ Secrets fetched and .env created"
EOF

chmod +x fetch-secrets.sh
./fetch-secrets.sh
```

### 2.4 Login to ECR and Pull Images

```bash
# Login to ECR
aws ecr get-login-password --region us-east-1 | \
    docker login --username AWS --password-stdin 123456789012.dkr.ecr.us-east-1.amazonaws.com

# Pull images (or build locally for first deployment)
docker compose pull
```

### 2.5 Start Services

```bash
docker compose up -d
```

### 2.6 Verify Deployment

```bash
# Check containers
docker compose ps

# Health check
curl http://localhost:8000/healthz

# Readiness check
curl http://localhost:8000/readyz

# View logs
docker compose logs -f
```

---

## Phase 3: GitHub Actions Setup (15 minutes)

### 3.1 Add GitHub Secrets

Go to: GitHub repo → Settings → Secrets and variables → Actions

Add these secrets:
- `AWS_ACCESS_KEY_ID`: Your AWS access key
- `AWS_SECRET_ACCESS_KEY`: Your AWS secret key
- `AWS_REGION`: `us-east-1`
- `ECR_REGISTRY`: Your ECR registry URL (e.g., `123456789012.dkr.ecr.us-east-1.amazonaws.com`)
- `SSH_PRIVATE_KEY`: Your EC2 SSH private key (entire contents)
- `PRODUCTION_HOST`: EC2 instance public IP

### 3.2 Test CI/CD Pipeline

```bash
# Make a test change
echo "# Test deployment" >> README.md

# Commit and push
git add README.md
git commit -m "test: CI/CD pipeline"
git push origin main
```

Watch the GitHub Actions workflow run and deploy to EC2.

---

## Phase 4: Testing (10 minutes)

### 4.1 Submit Test Receipt

```bash
curl -X POST http://$INSTANCE_IP:8000/v1/receipts \
  -H "Content-Type: application/json" \
  -d '{"image_url": "https://img-campaign.gotit.vn/scanit/mini-tet-2/2024-08-30/1724993296BBcQb_blob"}'

# Save the job_id from response
```

### 4.2 Poll for Results

```bash
curl http://$INSTANCE_IP:8000/v1/receipts/{JOB_ID}
```

### 4.3 Check Monitoring

- **Grafana**: http://$INSTANCE_IP:3000 (admin/admin)
- **Prometheus**: http://$INSTANCE_IP:9090

---

## Maintenance

### View Logs

```bash
# All services
docker compose logs -f

# Specific service
docker compose logs -f worker
docker compose logs -f api
docker compose logs -f triton
```

### Restart Services

```bash
docker compose restart
```

### Update Application

```bash
# Pull latest code
git pull origin main

# Pull new images (if using ECR)
docker compose pull

# Rolling restart
docker compose up -d
```

### Database Backup

```bash
# Manual backup
docker compose exec postgres pg_dump -U invoice invoice_ocr > backup.sql

# Restore
docker compose exec -T postgres psql -U invoice invoice_ocr < backup.sql
```

### Monitor Resource Usage

```bash
# Docker stats
docker stats

# Disk usage
df -h
du -sh /opt/invoice-ocr/*

# Memory
free -h
```

---

## Troubleshooting

### Issue: Worker not processing jobs

```bash
# Check worker logs
docker compose logs worker --tail 100

# Check Redis queue
docker compose exec redis redis-cli LLEN ocr:queue

# Restart workers
docker compose restart worker
```

### Issue: Triton not starting

```bash
# Check Triton logs
docker compose logs triton

# Verify model files
ls -lh models/yolov11n_receipt/1/model.onnx

# Check CPU mode in config
cat models/yolov11n_receipt/config.pbtxt | grep KIND_CPU
```

### Issue: Out of memory

```bash
# Check memory usage
free -h
docker stats

# Reduce worker concurrency
# Edit .env: WORKER_CONCURRENCY=2
docker compose up -d worker
```

### Issue: Database connection failures

```bash
# Check PostgreSQL
docker compose exec postgres pg_isready -U invoice

# Check connection string
docker compose exec api printenv POSTGRES_DSN

# Restart database
docker compose restart postgres
```

---

## Cost Optimization

### Reserved Instance (1-year commitment)

- **t3.xlarge Reserved**: ~$73/month (40% savings)
- **3-year commitment**: ~$48/month (60% savings)

### Spot Instance (for non-critical workloads)

- **t3.xlarge Spot**: ~$36/month (70% savings, but can be interrupted)

### Scheduled Scaling

For predictable traffic patterns, stop the instance during off-hours:

```bash
# Stop at night (via CloudWatch Events)
aws ec2 stop-instances --instance-ids $INSTANCE_ID

# Start in morning
aws ec2 start-instances --instance-ids $INSTANCE_ID
```

---

## Security Hardening

### 1. Restrict Security Group

```bash
# Remove 0.0.0.0/0 from API port
# Add only specific IPs that need access
aws ec2 revoke-security-group-ingress --group-id $SG_ID --protocol tcp --port 8000 --cidr 0.0.0.0/0
aws ec2 authorize-security-group-ingress --group-id $SG_ID --protocol tcp --port 8000 --cidr YOUR_APP_IP/32
```

### 2. Enable CloudWatch Logs

```bash
# Install CloudWatch agent
wget https://s3.amazonaws.com/amazoncloudwatch-agent/ubuntu/amd64/latest/amazon-cloudwatch-agent.deb
sudo dpkg -i amazon-cloudwatch-agent.deb
```

### 3. Enable AWS Systems Manager Session Manager

Allows SSH without exposing port 22:

```bash
# Install SSM agent (included in Amazon Linux 2)
snap install amazon-ssm-agent --classic
```

---

## Cost Breakdown

| Item | Monthly Cost |
|---|---|
| EC2 t3.xlarge (on-demand) | $121 |
| EBS 100GB gp3 | $10 |
| ECR storage (~10GB) | $1 |
| Data transfer (~100GB) | $9 |
| CloudWatch Logs (optional) | $2 |
| **Total** | **~$143** |

**Annual cost**: ~$1,716

**Compared to GPU (g4dn.xlarge)**: Save $273/month ($3,276/year)

---

## Next Steps

1. **Set up CloudWatch Alarms** - Alert on high CPU, memory, disk usage
2. **Configure domain + SSL** - Use AWS Certificate Manager + CloudFront/ALB
3. **Enable auto-scaling** - Add more EC2 instances behind ALB for high traffic
4. **Set up database backups** - Automated daily backups to S3
5. **Implement canary deployments** - Test new releases with 10% of traffic

---

For questions or issues, refer to the main [README.md](README.md) or create a GitHub issue.
