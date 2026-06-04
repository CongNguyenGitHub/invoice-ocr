# Invoice OCR — Gemini-powered Receipt Extraction (Experimental)

A high-throughput receipt OCR pipeline leveraging:
- **Triton Inference Server** with dynamic batching for YOLOv11n object detection (4–8 batch)
- **Google Gemini Flash Lite** via native `google-genai` SDK for structured field extraction
- **Redis LIST** for async task queue — fire-and-forget (no BLPOP wait)
- **External CDN** for image hosting — images are fetched by the worker from `img-campaign.gotit.vn`
- **PostgreSQL** for job persistence
- **4 worker processes × 4 async tasks** = 16 concurrent in-flight jobs per host

**Performance**: ~3 seconds p50 end-to-end on a single receipt; supports 10k/day steady, 30k burst via horizontal scaling.

---

## Architecture

```
Client → POST /v1/receipts {"image_url": "https://..."} → 202 Accepted
                    │
                    ▼
              Redis Queue ──────► Worker (downloads from CDN, runs pipeline)
                                       │
                                       ▼
                                  PostgreSQL (result stored)
```

The API **never downloads images**. It validates the URL domain, enqueues the job, and returns immediately. The worker picks up jobs, downloads images from the CDN, runs YOLO → Gemini extraction, and writes results to Postgres.

---

## Quick Start

### Prerequisites

- Docker + Docker Compose
- `.env` file (copy from `.env.example`):
  ```
  GEMINI_API_KEY=<your-api-key>
  GEMINI_MODEL=gemini-3.1-flash-lite-preview
  ```

### Local Stack

```bash
# Bring up all services (init, api, worker, triton, postgres, redis, prometheus, grafana)
docker compose up -d

# Verify readiness (all backends healthy)
curl http://localhost:8000/readyz
# {"ready":true,"redis":true,"postgres":true,"triton":true}
```

Wait ~30 seconds for the Triton YOLO model to load and workers to boot.

### Submit a Receipt

```bash
# Submit a CDN image URL (fire-and-forget — always returns 202)
curl -X POST http://localhost:8000/v1/receipts \
  -H "Content-Type: application/json" \
  -d '{"image_url": "https://img-campaign.gotit.vn/scanit/mini-tet-2/2024-08-30/1724993296BBcQb_blob"}'

# Response: 202 Accepted
# {"job_id": "550e8400-...", "status": "PENDING", "message": "Job enqueued"}
```

### Poll for Results

```bash
# Check job status
curl http://localhost:8000/v1/receipts/550e8400-e29b-41d4-a716-446655440000
```

---

## API Endpoints

### POST /v1/receipts

**Submit a receipt image URL for extraction.**

**Request:**
```bash
curl -X POST http://localhost:8000/v1/receipts \
  -H "Content-Type: application/json" \
  -d '{"image_url": "https://img-campaign.gotit.vn/scanit/..."}'
```

**Response (HTTP 202 — accepted):**
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "PENDING",
  "message": "Job enqueued"
}
```

**Response (HTTP 400 — invalid domain):**
```json
{
  "detail": "Domain not in allowlist: example.com. Allowed: img-campaign.gotit.vn"
}
```

**Response (HTTP 429 — queue full):**
```json
{
  "detail": "Queue backpressure: too many pending jobs"
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
  "error_code": "no_invoice_detected",
  "error_message": "YOLO detected no invoice in image"
}
```

---

### GET /healthz

**Liveness check** — returns 200 if the API is running.

### GET /readyz

**Readiness check** — returns 200 only if all dependencies are healthy (Redis, PostgreSQL, Triton).

---

## Configuration

All settings are environment variables (read from `.env` at startup):

| Variable | Default | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | *(required)* | Google Gemini API key |
| `GEMINI_MODEL` | `gemini-3.1-flash-lite-preview` | Model name |
| `GEMINI_TIMEOUT_SECONDS` | `15` | Per-call timeout |
| `PROMPT_SEMANTIC_VERSION` | `v3.7` | Prompt file version (cache key) |
| `POSTGRES_DSN` | `postgresql+asyncpg://...` | DB connection |
| `REDIS_URL` | `redis://redis:6379` | Redis connection |
| `TRITON_HOST` | `triton:8001` | Triton gRPC endpoint |
| `IMAGE_DOWNLOAD_TIMEOUT_SECONDS` | `30` | CDN download timeout |
| `IMAGE_DOWNLOAD_MAX_BYTES` | `10485760` | Max image size (10 MB) |
| `ALLOWED_IMAGE_DOMAINS` | `img-campaign.gotit.vn` | Domain allowlist for image URLs |
| `WORKER_CONCURRENCY` | `4` | Async tasks per worker process |
| `API_METRICS_PORT` | `9101` | Prometheus metrics (API) |
| `WORKER_METRICS_PORT` | `9102` | Prometheus metrics (Worker) |
| `JPEG_QUALITY` | `85` | JPEG encode quality |

---

## Monitoring

### Prometheus Metrics

Two separate metrics ports:

- **API** (port 9101): `curl http://localhost:9101/metrics`
  - `http_request_duration_seconds` — latency bucketed by endpoint
  - `ocr_api_queue_depth` — jobs waiting in Redis queue
  - `ocr_api_backpressure_rejects_total` — 429 responses when queue is full

- **Worker** (port 9102): `curl http://localhost:9102/metrics`
  - `ocr_cdn_download_seconds` — CDN image download latency
  - `ocr_triton_batch_size` — actual batch sizes sent to Triton
  - `ocr_phash_cache_hits_total` — cache hits
  - `ocr_phash_cache_misses_total` — cache misses
  - `gemini_retries_total` — Gemini retry attempts
  - `gemini_tokens_total` — prompt + response tokens consumed
  - `stale_jobs_recovered_total` — jobs reclaimed from dead workers

### Grafana Dashboards

Access at `http://localhost:3000` (admin / admin).

---

## Error Codes

| Code | Severity | Retry? | Meaning |
|---|---|---|---|
| `no_invoice_detected` | Permanent | ❌ | YOLO found no receipt in image |
| `prompt_missing` | Permanent | ❌ | `prompts/{PSV}.txt` not found |
| `gemini_api_key_missing` | Permanent | ❌ | `GEMINI_API_KEY` env var not set |
| `gemini_client_error` | Permanent | ❌ | Gemini returned 4xx error |
| `extractor_invalid_json` | Permanent | ❌ | Gemini returned unparseable JSON |
| `gemini_exhausted` | Transient | ✅ | Gemini 5xx/timeout after backoff retries |
| `database_unavailable` | Transient | ✅ | PostgreSQL connection failed |
| `cdn_download_failed` | Transient | ✅ | CDN image download failed |
| `rate_limited_locally` | Transient | ✅ | Gemini returned 429 |
| `stale_timeout` | Transient | ⚠️ | Job exceeded 15 min; sweeper marked failed |

---

## Troubleshooting

### All requests fail with 503 (database_unavailable)

```bash
docker compose logs postgres | tail -20
docker compose exec postgres psql -U invoice -d invoice_ocr -c "SELECT version();"
```

### Queue is always full (frequent 429 responses)

```bash
docker compose logs worker | grep "worker_task_started"
curl http://localhost:9101/metrics | grep ocr_api_queue_depth
```

### Worker crashes or doesn't start

```bash
docker compose logs worker --tail 50
docker compose build
docker compose down -v && docker compose up -d
```

---

## Example: End-to-End Flow

```bash
# 1. Submit receipt URL
JOB_ID=$(curl -s -X POST http://localhost:8000/v1/receipts \
  -H "Content-Type: application/json" \
  -d '{"image_url": "https://img-campaign.gotit.vn/scanit/mini-tet-2/2024-08-30/1724993296BBcQb_blob"}' \
  | jq -r '.job_id')
echo "Job: $JOB_ID"

# 2. Poll until done
for i in {1..15}; do
  RESULT=$(curl -s http://localhost:8000/v1/receipts/$JOB_ID)
  STATUS=$(echo $RESULT | jq -r '.status // .name')
  if [ "$STATUS" != "PENDING" ] && [ "$STATUS" != "PROCESSING" ]; then
    echo $RESULT | jq .
    break
  fi
  echo "[$i/15] Still processing..."
  sleep 2
done
```

---

## Deployment

### Docker Compose (development / single-host)

```bash
docker compose up -d
```

### Kubernetes (multi-host)

1. Build and push: `docker build -t myregistry/invoice-ocr:latest . && docker push ...`
2. Update `helm/values.yaml` with image, replicas, resources
3. Deploy: `helm install invoice-ocr ./helm --values helm/values.yaml`

---

## Support & Debugging

- **Logs**: `docker compose logs -f [service-name]`
- **Metrics**: `curl http://localhost:9101/metrics | grep ocr_`
- **Database**: `docker compose exec postgres psql -U invoice -d invoice_ocr -c "SELECT * FROM jobs LIMIT 5;"`
- **Cache**: `docker compose exec redis redis-cli KEYS "ocr:*" | head -20`

---

**Version**: 4.0.0  
**Last updated**: 2026-06-04  
**Maintainer**: Your team
