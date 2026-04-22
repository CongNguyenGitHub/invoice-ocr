# Invoice OCR — Gemini-powered Receipt Extraction

A high-throughput, low-latency receipt OCR pipeline leveraging:
- **Triton Inference Server** with dynamic batching for YOLOv11n object detection (4–8 batch)
- **Google Gemini Flash Lite** via native `google-genai` SDK for structured field extraction
- **Redis LIST + BLPOP RPC** for async task queue (no Celery)
- **PostgreSQL + MinIO** for persistence and file storage
- **4 worker processes × 4 async tasks** = 16 concurrent in-flight jobs per host

**Performance**: ~3 seconds p50 end-to-end on a single receipt; supports 10k/day steady, 30k burst via horizontal scaling.

---

## Quick Start

### Prerequisites

- Docker + Docker Compose
- `.env` file with:
  ```
  GEMINI_API_KEY=<your-api-key>
  GEMINI_MODEL=gemini-3.1-flash-lite-preview
  ```

### Local Stack

```bash
# Bring up all services (init, api, worker, triton, postgres, redis, minio, prometheus, grafana)
docker compose up -d

# Verify readiness (all backends healthy)
curl http://localhost:8000/readyz
# {"ready":true,"redis":true,"postgres":true,"minio":true,"triton":true}
```

Wait ~30 seconds for the Triton YOLO model to load and workers to boot.

### Test with a Receipt

```bash
# Submit a receipt image (synchronous ingress, async backend)
curl -F file=@path/to/receipt.jpg http://localhost:8000/v1/receipts

# Response: 200 (success) or 202 (still processing) or 422 (permanent failure)
```

---

## API Endpoints

### POST /v1/receipts

**Submit a receipt image for extraction.**

**Request:**
```bash
curl -F file=@receipt.jpg http://localhost:8000/v1/receipts
```

**Response (HTTP 200 — success):**
```json
{
  "name": "AEON",
  "type": "supermarket",
  "date": "2024-08-15",
  "time": "18:45",
  "pos_id": "POS123",
  "receipt_number": "000789",
  "cashier": "001",
  "total_money": "1356000",
  "barcode": "000007069192",
  "products": [
    {
      "product_id": "1",
      "product_name": "Heineken Silver 3 cans",
      "product_unit_price": "150000",
      "product_quantity": "3",
      "product_discount_money": "0",
      "product_total_money": "452000"
    }
  ]
}
```

**Response (HTTP 202 — processing):**
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "PENDING"
}
```
Poll `GET /v1/receipts/{job_id}` every 1–2 seconds until 200 or 422.

**Response (HTTP 422 — permanent failure):**
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "FAILED_PERMANENT",
  "error_code": "no_invoice_detected",
  "error_message": "YOLO detected no invoice in image"
}
```

**Response (HTTP 503 — transient failure, retry):**
```json
{
  "error": "Service temporarily unavailable",
  "details": "redis_unavailable"
}
```

---

### GET /v1/receipts/{job_id}

**Poll job status or retrieve the result.**

**Response (HTTP 200 — complete):**
```json
{
  "name": "AEON",
  "type": "supermarket",
  ...
}
```

**Response (HTTP 202 — still processing):**
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "PROCESSING"
}
```

**Response (HTTP 422 — permanent failure):**
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "FAILED_PERMANENT",
  "error_code": "gemini_client_error",
  "error_message": "Gemini returned 4xx error"
}
```

**Response (HTTP 503 — stale/orphaned job):**
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "FAILED_PERMANENT",
  "error_code": "stale_timeout",
  "error_message": "Job processing exceeded 15 minutes; worker may have crashed"
}
```

---

### GET /healthz

**Liveness check** — returns 200 if the API is running.

```bash
curl http://localhost:8000/healthz
# 200 OK
```

---

### GET /readyz

**Readiness check** — returns 200 only if all dependencies are healthy (Redis, PostgreSQL, MinIO, Triton).

```bash
curl http://localhost:8000/readyz
# {"ready":true,"redis":true,"postgres":true,"minio":true,"triton":true}
```

Use this to gate traffic before full startup.

---

## Configuration

All settings are environment variables (read from `.env` at startup):

| Variable | Default | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | *(required)* | Google Gemini API key |
| `GEMINI_MODEL` | `gemini-3.1-flash-lite-preview` | Model name |
| `GEMINI_TIMEOUT_SECONDS` | `15` | Per-call timeout |
| `PROMPT_SEMANTIC_VERSION` | `v3.4` | Prompt file version (cache key) |
| `POSTGRES_DSN` | `postgresql+asyncpg://invoice:invoice@postgres:5432/invoice_ocr` | DB connection |
| `REDIS_URL` | `redis://redis:6379` | Redis connection |
| `MINIO_ENDPOINT` | `minio:9000` | MinIO S3-compatible server |
| `MINIO_ACCESS_KEY` | `minioadmin` | MinIO user |
| `MINIO_SECRET_KEY` | `minioadmin` | MinIO password |
| `TRITON_HOST` | `triton:8001` | Triton gRPC endpoint |
| `WORKER_CONCURRENCY` | `4` | Async tasks per worker process |
| `API_METRICS_PORT` | `9101` | Prometheus metrics (API) |
| `WORKER_METRICS_PORT` | `9102` | Prometheus metrics (Worker) |
| `WHITELIST_RELOAD_INTERVAL` | `60` | Seconds between whitelist file checks |
| `JPEG_QUALITY` | `85` | JPEG encode quality |

### Prompt Version (PSV) Bumps

When rolling out a new prompt:

1. **Stage A (strict=false):**
   - Add `prompts/v3.5.txt`
   - Set `PROMPT_SEMANTIC_VERSION=v3.5` in `.env`
   - Restart workers
   - Monitor `ocr_phash_cache_misses_total` — should spike as new cache keys are generated
   - Run smoke tests to validate accuracy

2. **Stage B (strict=true):**
   - After 24 hours of Stage A, remove traffic fallback: redeploy without Stage A allowlist

Old cache entries expire by TTL (90 days default) and are never re-read.

---

## Monitoring

### Prometheus Metrics

Two separate metrics ports:

- **API** (port 9101): `curl http://localhost:9101/metrics`
  - `http_request_duration_seconds` — latency bucketed by endpoint
  - `ocr_api_queue_depth` — jobs waiting in Redis queue
  - `ocr_api_backpressure_rejects_total` — 429 responses when queue is full

- **Worker** (port 9102): `curl http://localhost:9102/metrics`
  - `ocr_triton_batch_size` — actual batch sizes sent to Triton
  - `ocr_phash_cache_hits_total` — cache hits (re-extracted by ID)
  - `ocr_phash_cache_misses_total` — cache misses (new extractions)
  - `gemini_retries_total` — Gemini retry attempts
  - `gemini_tokens_total` — prompt + response tokens consumed
  - `fail_side_effect_errors_total` — failures during error handling (rare)
  - `stale_jobs_recovered_total` — jobs reclaimed from dead workers

### Grafana Dashboards

Access at `http://localhost:3000` (admin / admin):

- **Panel 1**: Request latency (p50/p95/p99) over time
- **Panel 2**: Queue depth + backpressure rejections
- **Panel 3**: Triton batch size distribution
- **Panel 4**: Gemini token consumption
- **Panel 5**: Cache hit rate
- **Panel 6**: Error rate by `error_code`

---

## Error Codes

| Code | Severity | Retry? | Meaning |
|---|---|---|---|
| `no_invoice_detected` | Permanent | ❌ | YOLO found no receipt in image; crop/orientation issue |
| `prompt_missing` | Permanent | ❌ | `prompts/{PSV}.txt` not found; configuration error |
| `gemini_api_key_missing` | Permanent | ❌ | `GEMINI_API_KEY` env var not set |
| `gemini_client_error` | Permanent | ❌ | Gemini returned 4xx error (schema mismatch, invalid key, quota) |
| `extractor_invalid_json` | Permanent | ❌ | Gemini returned unparseable JSON |
| `gemini_exhausted` | Transient | ✅ | Gemini 5xx/timeout after 3 backoff retries |
| `database_unavailable` | Transient | ✅ | PostgreSQL connection failed |
| `storage_transient` | Transient | ✅ | MinIO/S3 temporarily unavailable |
| `rate_limited_locally` | Transient | ✅ | Gemini returned 429 (rate limit) |
| `stale_timeout` | Transient | ⚠️ | Job processing exceeded 15 minutes; sweeper marked as failed |

**Retry strategy**: 202 responses or transient codes → wait 1–2 seconds, retry the same `GET /v1/receipts/{job_id}`.

---

## Troubleshooting

### All requests fail with 503 (database_unavailable)

```bash
# Check PostgreSQL
curl http://localhost:5432
docker compose logs postgres | tail -20

# Verify .env POSTGRES_DSN matches the running container
docker compose exec postgres psql -U invoice -d invoice_ocr -c "SELECT version();"
```

### Queue is always full (frequent 429 responses)

```bash
# Check if workers are consuming
docker compose logs worker | grep "worker_task_started"

# Monitor queue depth in Prometheus
curl http://localhost:9101/metrics | grep ocr_api_queue_depth

# If workers are healthy but queue grows:
#   → Workers may be slow on Gemini calls
#   → Increase WORKER_CONCURRENCY in .env (default 4)
#   → Add more worker containers via docker-compose replica scaling
```

### Gemini always returns 400 (schema mismatch)

```bash
# Verify the response_schema is valid for the current Gemini model
docker compose exec worker python -c "from src.pipeline.json_schema import LEGACY_JSON_SCHEMA; import json; print(json.dumps(LEGACY_JSON_SCHEMA, indent=2))" | head -20

# Ensure `additionalProperties` key is NOT present (Gemini rejects it)
# The fix: Pydantic's extra="forbid" on InvoiceResult handles drift detection client-side
```

### Extract quality is poor (wrong fields)

```bash
# Check which prompt version is active
grep PROMPT_SEMANTIC_VERSION .env

# Review the prompt file
cat src/pipeline/prompts/v3.4.txt

# Test with a sample receipt
python -c "
import sys
sys.path.insert(0, '.')
from src.pipeline.preprocessor import preprocess
from PIL import Image

img = Image.open('path/to/receipt.jpg')
pp = preprocess(img)
print(f'Invoice detected: {pp.invoice_crop.size}')
print(f'pHash: {pp.phash}')
"

# If detection is wrong, tune YOLO thresholds in src/pipeline/detector.py
```

### Worker crashes or doesn't start

```bash
# Check logs
docker compose logs worker --tail 50

# Verify all images are built
docker compose build

# Recreate from scratch (warning: clears jobs)
docker compose down -v
docker compose up -d

# Check if Triton is ready
docker compose logs triton | grep "READY"
```

### Cached results are stale (prompt changed but still getting old output)

```bash
# Check cache TTL
docker compose exec redis redis-cli SCAN 0 MATCH "ocr:phash:*"

# Clear cache for a specific pHash (from GET response body)
docker compose exec redis redis-cli DEL "ocr:phash:c3c0276aaf787368:psv:v3.4"

# Or flush all cache (warning: will re-extract all pending results)
docker compose exec redis redis-cli FLUSHDB
```

---

## Example: End-to-End Flow

```bash
# 1. Submit receipt
JOB_ID=$(curl -F file=@sample.jpg http://localhost:8000/v1/receipts | jq -r '.job_id // "immediate"')
echo "Job: $JOB_ID"

# 2. If synchronous (immediate 200):
curl http://localhost:8000/v1/receipts/$JOB_ID | jq .

# 3. If asynchronous (202):
for i in {1..10}; do
  STATUS=$(curl -s http://localhost:8000/v1/receipts/$JOB_ID | jq -r '.status // .name')
  if [ "$STATUS" != "PENDING" ] && [ "$STATUS" != "PROCESSING" ]; then
    curl http://localhost:8000/v1/receipts/$JOB_ID | jq .
    break
  fi
  echo "[$i/10] Still processing..."
  sleep 2
done
```

---

## Load Testing

Run the included smoke test against real ground-truth receipts:

```bash
# Extract first 10 receipts and compare vs labels
python scripts/smoke_e2e.py --n 10 --max-wait 180

# View results
cat smoke_report.json | jq '.pass_strict, .total'
```

For sustained load:

```bash
# Requires scripts/test_burst.py
# python scripts/test_burst.py --rps 8 --duration 120
```

---

## CI Gates

Three GitHub Actions workflows gate every change before merge:

| Workflow                    | Trigger                                        | Time   | What it checks                                                                                          |
|-----------------------------|------------------------------------------------|--------|---------------------------------------------------------------------------------------------------------|
| **fast-checks.yml**         | Every PR + push to main                        | ~2 min | `ruff`, `mypy` (warn-only), `pytest tests/unit tests/integration` (57 tests: schema, replay, backpressure math) |
| **stack-gate.yml**          | PRs touching `src/**`, `whitelists/**`, compose, prompt, eval scripts | ~15 min | Docker stack up → `ci_load_test.sh` (p95<10s, p99<30s, err<1%) → `run_eval.py` on 120 test records → `check_accuracy.py --mode smoke` |
| **full-eval.yml**           | Nightly 03:07 UTC + release tags `v*` + manual | ~25 min | Full 400-record test-set eval → `check_accuracy.py --mode strict`; on release tag also bumps baseline & commits |

### Accuracy gate — how to read it

The gate compares a new eval report against `experiments/baseline.json` on three layers:

1. **Absolute floor** — overall and per-field accuracy must exceed configured floors.
2. **Relative drop** — overall/field accuracy must not drop more than `*_relative_drop_max` pp
   below the reference run.
3. **Per-store floor** — average of seven primary fields (`name`, `date`, `total_money`,
   `receipt_number`, `pos_id`, `cashier`, `product_name`) per store type must exceed its floor.
   Store types with fewer than 10 records are excluded.

Two modes tune the strictness:

- `--mode smoke` (default, PR gate): floors relaxed by −2 pp, drop tolerances × 1.5 — absorbs
  the ~1–2 pp sampling noise of the 120-record smoke slice.
- `--mode strict` (nightly + release): raw baseline values, no relaxation.

### Bumping the baseline

The baseline is manually pinned. Only bump on a deliberate release:

```bash
# Tag the release and push — full-eval.yml will run, then commit the baseline bump
git tag v3.8 && git push origin v3.8
```

To bump locally against an ad-hoc eval report (e.g. for debugging):

```bash
python scripts/check_accuracy.py \
    --report eval_reports/eval_report_v3.8_test_*.json \
    --baseline experiments/baseline.json \
    --bump-baseline
```

New floors are set `observed_pct − 2 pp` (per field) and `observed − 3 pp` (per store), so the
next CI run has breathing room.

### Running the gate locally

```bash
# 1) Fast path (no Docker, no Gemini)
pre-commit install                                   # one time
pytest tests/unit tests/integration -v               # must be green

# 2) Full stack + smoke eval (costs ~$0.50 in Gemini calls)
docker compose up -d
bash scripts/ci_load_test.sh                         # p95<10s, p99<30s, err<1%
python scripts/run_eval.py \
    --input data/eval/test_set.json \
    --max-records 120 \
    --out eval_reports/
python scripts/check_accuracy.py \
    --report 'eval_reports/eval_report_*_test_*.json' \
    --baseline experiments/baseline.json \
    --mode smoke                                     # exit 0 = pass
docker compose down -v
```

### Required GitHub secrets

| Secret / variable      | Purpose                                                 |
|------------------------|---------------------------------------------------------|
| `GEMINI_API_KEY_CI`    | Rate-limited Gemini key for CI runs (secret)            |
| `PROMPT_SEMANTIC_VERSION` | Active PSV, e.g. `v3.7` (repository variable)        |

### What the gate does NOT cover (yet)

- **Soak test** — load_test.py runs 3 min; long-tail memory leaks need a separate weekly workflow
- **Chaos test** — kill-worker-mid-job regression is not automated
- **Prompt-injection resistance** — all current input is internal retail gold
- **Container vuln scan** — orthogonal; add Trivy when security review demands
- **Cost/token budget alerts** — `ocr_gemini_tokens_total` is exported but not CI-gated

---

## Deployment

### Docker Compose (development / single-host)

```bash
docker compose up -d
```

### Kubernetes (multi-host, production)

1. Build and push images to your registry:
   ```bash
   docker build -t myregistry/invoice-ocr:latest .
   docker push myregistry/invoice-ocr:latest
   ```

2. Update `helm/values.yaml` with:
   - Image: `myregistry/invoice-ocr:latest`
   - Replicas: `api: 2, worker: 4`
   - Resources: CPU/memory requests/limits
   - Persistence: PVC for postgres / minio volumes

3. Deploy:
   ```bash
   helm install invoice-ocr ./helm --values helm/values.yaml
   ```

4. Verify:
   ```bash
   kubectl get pods -l app=invoice-ocr
   kubectl logs -f deployment/invoice-ocr-api
   ```

---

## Support & Debugging

- **Logs**: `docker compose logs -f [service-name]`
- **Metrics**: `curl http://localhost:9101/metrics | grep ocr_`
- **Database**: `docker compose exec postgres psql -U invoice -d invoice_ocr -c "SELECT * FROM jobs LIMIT 5;"`
- **Cache**: `docker compose exec redis redis-cli KEYS "ocr:*" | head -20`
- **Files**: `docker compose exec minio ls -la /data/ocr_*`

---

**Version**: 3.0.0  
**Last updated**: 2026-04-20  
**Maintainer**: Your team
