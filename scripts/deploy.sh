#!/bin/bash
#
# Simple deployment script for single EC2 instance
# Usage: ./deploy.sh <image_tag>
#
# Example: ./deploy.sh 123456789012.dkr.ecr.us-east-1.amazonaws.com/invoice-ocr:sha-abc123
#

set -euo pipefail

IMAGE=${1:-}

if [ -z "$IMAGE" ]; then
    echo "Error: IMAGE argument required"
    echo "Usage: $0 <image_tag>"
    echo "Example: $0 123456789012.dkr.ecr.us-east-1.amazonaws.com/invoice-ocr:sha-abc123"
    exit 1
fi

echo "=========================================="
echo "Deploying invoice-ocr"
echo "Image: $IMAGE"
echo "=========================================="

# Export IMAGE for docker-compose
export IMAGE

# Pull new image
echo ""
echo "Pulling new image..."
docker compose pull

# Rolling restart (docker-compose handles this automatically)
echo ""
echo "Restarting services..."
docker compose up -d

# Wait for services to stabilize
echo ""
echo "Waiting for services to start..."
sleep 10

# Health check
echo ""
echo "Running health check..."
if curl -f -s http://localhost:8000/healthz | jq .; then
    echo ""
    echo "✓ Deployment successful"
    echo ""
    echo "Container status:"
    docker compose ps
    exit 0
else
    echo ""
    echo "✗ Health check failed"
    echo ""
    echo "Container logs (last 50 lines):"
    docker compose logs --tail=50
    exit 1
fi
