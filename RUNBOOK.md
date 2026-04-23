# Runbook — Invoice OCR v3

## 1. Stuck jobs

Symptom: `GET /v1/receipts/{id}` returns 202 for > 16 min.

- The sweeper should promote to `FAILED_TRANSIENT` with `error_code=stale_timeout`
  after 15 min PROCESSING or 30 min PENDING.
- If it does not, inspect the sweeper logs (one worker per process runs it):
  `docker compose logs worker | grep sweeper_`
- Manually drain:
  ```sql
  UPDATE jobs SET status='FAILED_TRANSIENT', error_code='manual_drain',
                  updated_at=now()
   WHERE status='PROCESSING' AND updated_at < now() - interval '20 minutes';
  ```

## 2. Gemini outage

Symptom: `ocr_gemini_retries_total{attempt!="1"}` spike, `/v1/receipts`
returning 503.

- Extractor backs off 3 × (0.3, 0.6, 1.2)s per attempt then raises
  `GeminiExhaustedError` → `FAILED_TRANSIENT`. The worker will requeue
  up to 3 times (bounded).
- If sustained, lower `TOKEN_BUCKET_RPS` via Redis:
  `redis-cli HSET ocr:rate_limit_config rps 1 burst 2`
- Workers pick this up within `RATE_LIMIT_REFRESH_INTERVAL` (30 s).

## 3. Triton outage

Symptom: YOLO stage errors, detector raises `TritonUnavailableError`.

- `docker compose restart triton`
- Verify model ready: `curl http://localhost:8000/v2/models/yolov11n_receipt/ready`
- Workers treat this as transient → requeue up to 3×.

## 4. Redis blip

Symptom: brief connection loss. No action usually needed — `wait_for_result`
swallows `ConnectionError` and falls through to 504+poll. `pop_from_queue`
returns None. Sweeper reclaims any in-flight rows.

## 5. PSV bump (strangler)

Two-stage rollout:

**Stage A (observe):** Set `PROMPT_SEMANTIC_VERSION=v3.5`, place
`prompts/v3.5.txt`, keep `strict=false` in `LEGACY_JSON_SCHEMA` (or add new
optional fields with `extra="ignore"` in pydantic). Watch
`ocr_new_field_present_total`.

**Stage B (enforce):** Flip schema to `strict=true` /
`additionalProperties:false` (already the default). Old `v3.4` cache keys
age out naturally (86400 s TTL).

## 6. Graceful SIGTERM

API and worker install SIGTERM handlers. Worker drains in-flight jobs;
API lifespan cancels sampler and closes pools.

---

## 7. Prod box died — rebuild from scratch

Total recovery time: **~12 minutes**. RPO ≤ 1 hour (last EBS snapshot).

```bash
# 1. New EC2 + IAM + SG (idempotent — reuses key/SG/role if already exist)
ENV=prod bash scripts/aws/provision-vps.sh

# 2. Find latest hourly snapshot of the dead volume
SNAP=$(aws --profile invoice-ocr ec2 describe-snapshots --owner-ids self \
  --filters Name=tag:Project,Values=invoice-ocr Name=tag:Env,Values=prod \
            Name=tag:AutoSnapshot,Values=true \
  --query 'Snapshots | sort_by(@, &StartTime) | [-1].SnapshotId' --output text)
echo "Restoring from $SNAP"

# 3. Detach the new instance's blank EBS, replace with snapshot-restored volume
INST=$(aws --profile invoice-ocr ec2 describe-instances \
  --filters Name=tag:Name,Values=invoice-ocr-prod Name=instance-state-name,Values=running \
  --query 'Reservations[0].Instances[0].InstanceId' --output text)
AZ=$(aws --profile invoice-ocr ec2 describe-instances --instance-ids $INST \
  --query 'Reservations[0].Instances[0].Placement.AvailabilityZone' --output text)
NEWVOL=$(aws --profile invoice-ocr ec2 create-volume --snapshot-id $SNAP \
  --volume-type gp3 --availability-zone $AZ \
  --tag-specifications "ResourceType=volume,Tags=[{Key=Project,Value=invoice-ocr},{Key=Env,Value=prod}]" \
  --query 'VolumeId' --output text)
aws --profile invoice-ocr ec2 wait volume-available --volume-ids $NEWVOL

# Stop the instance, swap the root volume, restart
aws --profile invoice-ocr ec2 stop-instances --instance-ids $INST
aws --profile invoice-ocr ec2 wait instance-stopped --instance-ids $INST
OLDVOL=$(aws --profile invoice-ocr ec2 describe-instances --instance-ids $INST \
  --query 'Reservations[0].Instances[0].BlockDeviceMappings[0].Ebs.VolumeId' --output text)
aws --profile invoice-ocr ec2 detach-volume  --volume-id $OLDVOL
aws --profile invoice-ocr ec2 wait volume-available --volume-ids $OLDVOL
aws --profile invoice-ocr ec2 attach-volume  --volume-id $NEWVOL --instance-id $INST --device /dev/xvda
aws --profile invoice-ocr ec2 start-instances --instance-ids $INST
aws --profile invoice-ocr ec2 wait instance-running --instance-ids $INST

# 4. Bootstrap the box (re-deploys the latest production image)
DNS=$(aws --profile invoice-ocr ec2 describe-instances --instance-ids $INST \
  --query 'Reservations[0].Instances[0].PublicDnsName' --output text)
scp -i ~/.ssh/invoice-ocr-prod-key.pem \
    scripts/aws/bootstrap-vps.sh ec2-user@$DNS:/tmp/
ssh -i ~/.ssh/invoice-ocr-prod-key.pem ec2-user@$DNS \
    'sudo ENV=prod bash /tmp/bootstrap-vps.sh'

# 5. Verify
ssh -i ~/.ssh/invoice-ocr-prod-key.pem ec2-user@$DNS 'make -C /opt/invoice-ocr health'
```

## 8. Restoring Postgres only (data corruption, image fine)

```bash
ssh ec2-user@$PROD_HOST
cd /opt/invoice-ocr
sudo make rollback                # try image rollback first

# If still bad — restore postgres data directory from snapshot
sudo systemctl stop invoice-ocr   # stop the stack cleanly
docker volume rm invoice-ocr_postgres_data
# Mount the snapshot (manually attach an EBS restored from snapshot,
#  or just `docker run -v invoice-ocr_postgres_data:/restore postgres:16-alpine
#  ... restore from a logical pg_dump if you have one`)
sudo systemctl start invoice-ocr
make health
```

## 9. Rotating GEMINI_API_KEY without downtime

```bash
# 1. Push the new key to SSM (existing param overwritten)
echo 'GEMINI_API_KEY=NEW_KEY' > .env.prod
ENV=prod bash scripts/aws/seed-secrets.sh

# 2. SSH the box, refresh .env, restart only the workers (api doesn't call Gemini)
ssh ec2-user@$PROD_HOST <<'REMOTE'
cd /opt/invoice-ocr
sudo make pull-secrets       # rewrites .env
sudo docker compose up -d worker --force-recreate
REMOTE

# 3. Verify in Grafana that ocr_gemini_retries_total stays flat
# 4. Revoke the old key in the Gemini console
```
