# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Invoice OCR pipeline using Triton-batched YOLOv11n + Google Gemini Flash Lite for receipt field extraction. Fire-and-forget async architecture with Redis queue, PostgreSQL persistence, and external CDN image hosting.

**Performance**: ~3s p50 end-to-end, 10k/day steady state, 30k burst via horizontal scaling.

## Architecture Principles

### Fire-and-Forget Pattern
- API **never downloads images** — only validates URL domain, creates job record, enqueues to Redis, returns 202
- Worker downloads from CDN (`img-campaign.gotit.vn`), runs pipeline, writes result to Postgres
- Client polls GET `/v1/receipts/{job_id}` for results

### Worker Concurrency Model
- 4 worker processes (docker-compose `replicas: 4`)
- Each process runs 4 async tasks (`WORKER_CONCURRENCY=4`)
- Total: **16 concurrent in-flight jobs per host**

### CDN Image URLs
- Images are hosted externally on `img-campaign.gotit.vn`
- API validates domain allowlist before accepting jobs (`ALLOWED_IMAGE_DOMAINS`)
- Worker fetches images via HTTP (`src/storage/http_client.py`)

### Status Mirroring HTTP Codes
- GET `/v1/receipts/{job_id}` mirrors job status to HTTP codes:
  - `200` → `SUCCEEDED` (bare InvoiceResult JSON)
  - `202` → `PENDING`/`PROCESSING` (envelope with status)
  - `422` → `FAILED_PERMANENT` (client error, don't retry)
  - `503` → `FAILED_TRANSIENT` (retry later)
  - `404` → job_id unknown

### Triton Dynamic Batching
- YOLOv11n runs on Triton Inference Server with dynamic batching (batch size 4–8)
- Worker sends gRPC requests; Triton queues and batches them automatically
- See `src/pipeline/triton_client.py` and `src/pipeline/detector.py`

### Postprocessor Always Runs
- Normalization (dates, money, unicode) runs on **every result**, including cache hits
- Whitelist fuzzy matching (store names, product names) via `rapidfuzz`
- See `src/pipeline/postprocessor.py` and `src/pipeline/whitelist_index.py`

### Token Bucket Rate Limiting
- Gemini API calls are rate-limited via token bucket (`TOKEN_BUCKET_RPS=4.0`, `TOKEN_BUCKET_BURST=8`)
- Bucket refills every `RATE_LIMIT_REFRESH_INTERVAL=30s`
- Workers share bucket state via Redis

## Common Development Commands

### Local Stack
```bash
# Start all services (API, Worker, Triton, Postgres, Redis, Prometheus, Grafana)
docker compose up -d

# View logs
docker compose logs -f [api|worker|triton|postgres|redis]

# Rebuild and restart after code changes
docker compose build
docker compose up -d

# Tear down and reset volumes
docker compose down -v
```

### Testing
```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run all tests
pytest

# Run specific test modules
pytest tests/unit/test_m2_pipeline_core.py
pytest tests/integration/

# Run with verbose output
pytest -v -s
```

### Linting & Type Checking
```bash
# Ruff format and lint
ruff check .
ruff format .

# Type checking
mypy src/
```

### Database Migrations
```bash
# Run migrations (inside init container or manually)
alembic upgrade head

# Create new migration
alembic revision --autogenerate -m "description"

# Rollback one migration
alembic downgrade -1
```

### Monitoring
```bash
# API metrics (Prometheus format)
curl http://localhost:9101/metrics

# Worker metrics
curl http://localhost:9102/metrics

# Grafana dashboards
open http://localhost:3000  # admin / admin
```

### Smoke Test
```bash
# Submit a test receipt
curl -X POST http://localhost:8000/v1/receipts \
  -H "Content-Type: application/json" \
  -d '{"image_url": "https://img-campaign.gotit.vn/scanit/mini-tet-2/2024-08-30/1724993296BBcQb_blob"}'

# Poll for result (replace JOB_ID)
curl http://localhost:8000/v1/receipts/{JOB_ID}
```

## Key Code Locations

### API Layer
- **`src/api/routes.py`** — POST/GET endpoints, domain validation, backpressure checks
- **`src/api/backpressure.py`** — Redis queue depth checks, 429 responses
- **`src/api/metrics.py`** — Prometheus metrics (API)

### Worker Layer
- **`src/worker/main.py`** — Worker entrypoint, task spawning, signal handlers
- **`src/worker/loop.py`** — Task lifecycle: pop from queue → download → detect → extract → postprocess → persist
- **`src/worker/sweeper.py`** — Background daemon that reclaims stale jobs stuck in PROCESSING
- **`src/worker/metrics.py`** — Prometheus metrics (Worker)

### Pipeline Components
- **`src/pipeline/detector.py`** — YOLO via Triton gRPC, bounding box detection
- **`src/pipeline/extractor.py`** — Gemini API call with structured JSON schema
- **`src/pipeline/postprocessor.py`** — Date/money/unicode normalization, whitelist fuzzy matching
- **`src/pipeline/preprocessor.py`** — Image resize/JPEG compression before Triton
- **`src/pipeline/whitelist_index.py`** — In-memory frozen whitelist for store/product name matching
- **`src/pipeline/prompts/`** — Prompt templates versioned by `PROMPT_SEMANTIC_VERSION`

### Storage Clients
- **`src/storage/postgres_client.py`** — AsyncPG connection pool, job CRUD operations
- **`src/storage/redis_client.py`** — Redis LIST queue, phash cache, requeue tracking
- **`src/storage/http_client.py`** — HTTP client for downloading images from CDN

### Configuration & Schemas
- **`src/config/settings.py`** — Single source of truth for all environment variables (pydantic-settings)
- **`src/schemas/invoice.py`** — Pydantic models for `InvoiceResult`, `Product`, etc.
- **`src/domain/errors.py`** — Custom exceptions with severity (permanent vs transient)
- **`src/domain/constants.py`** — Enums for `JobStatus`, `ErrorCode`, etc.

### Initialization & Migrations
- **`src/init/entrypoint.py`** — Init container: runs Alembic migrations, waits for backends
- **`migrations/versions/`** — Alembic migration files

## Testing Strategy

### Unit Tests (`tests/unit/`)
- **`test_m0_skeleton.py`** — Smoke tests for imports and basic instantiation
- **`test_m1_storage_surface.py`** — Redis/Postgres client mocks
- **`test_m2_pipeline_core.py`** — Detector, extractor, postprocessor logic
- **`test_m5_api_surface.py`** — FastAPI route tests with TestClient

### Integration Tests (`tests/integration/`)
- End-to-end tests with real Redis/Postgres (requires `docker compose up -d`)

### Contract Tests (`tests/contract/`)
- JSON schema validation for Gemini responses

## Environment Variables

All configuration is via environment variables (`.env` file, loaded by `src/config/settings.py`):

**Critical:**
- `GEMINI_API_KEY` — Google Gemini API key (required)
- `POSTGRES_DSN` — PostgreSQL connection string
- `REDIS_URL` — Redis connection URL
- `ALLOWED_IMAGE_DOMAINS` — Domain allowlist for image URLs (comma-separated)

**Pipeline:**
- `TRITON_HOST` — Triton gRPC endpoint (default: `triton:8001`)
- `GEMINI_MODEL` — Model name (default: `gemini-3.1-flash-lite-preview`)
- `PROMPT_SEMANTIC_VERSION` — Prompt version for cache invalidation (default: `v3.7`)

**Worker:**
- `WORKER_CONCURRENCY` — Async tasks per process (default: `4`)
- `TOKEN_BUCKET_RPS` — Gemini rate limit (default: `4.0`)
- `WORKER_ID` — Unique worker identifier for distributed sweeping

**Observability:**
- `API_METRICS_PORT` — Prometheus metrics port for API (default: `9101`)
- `WORKER_METRICS_PORT` — Prometheus metrics port for Worker (default: `9102`)

See `.env.example` for full list.

## Error Codes

Errors are classified by **severity** (permanent vs transient) to guide retry logic:

**Permanent (don't retry):**
- `no_invoice_detected` — YOLO found no receipt in image
- `prompt_missing` — Prompt file not found
- `gemini_client_error` — Gemini 4xx error
- `extractor_invalid_json` — Gemini returned unparseable JSON

**Transient (retry):**
- `cdn_download_failed` — CDN timeout or network error
- `gemini_exhausted` — Gemini 5xx/timeout after retries
- `rate_limited_locally` — Gemini 429
- `database_unavailable` — Postgres connection failed
- `stale_timeout` — Job exceeded 15 min (sweeper reclaimed it)

See `src/domain/errors.py` for full list and exception hierarchy.

## Whitelist Fuzzy Matching

Store and product names are normalized via fuzzy matching against frozen whitelists:
- **Location**: `whitelists/store_names_whitelist.json`, `whitelists/product_names_whitelist.json` (mounted read-only in worker)
- **Format**: JSON arrays of string entries
- **Algorithm**: `rapidfuzz.process.extractOne` with score threshold
- **When**: Postprocessor runs on every result (including cache hits)
- **Implementation**: `src/pipeline/whitelist_index.py`

To update whitelists:
1. Edit the JSON whitelist files in `whitelists/`
2. Rebuild worker: `docker compose build worker`
3. Restart: `docker compose up -d worker`

## Sweeper Daemon

Background task that reclaims stale jobs stuck in `PROCESSING` status:
- **Location**: `src/worker/sweeper.py`
- **Runs**: Inside every worker process (distributed locking via Redis)
- **Interval**: Every `SWEEP_INTERVAL_SECONDS=60s`
- **Threshold**: Jobs in PROCESSING > `STALE_PROCESSING_MINUTES=15` are marked `FAILED_TRANSIENT` and re-queued (up to `REQUEUE_MAX=3` times)
- **Why**: Handles worker crashes, out-of-memory kills, network partitions

## Prometheus Metrics

### API Metrics (port 9101)
- `http_request_duration_seconds{endpoint}` — Latency histogram per endpoint
- `ocr_api_queue_depth` — Current Redis queue length
- `ocr_api_backpressure_rejects_total` — Count of 429 responses

### Worker Metrics (port 9102)
- `ocr_cdn_download_seconds` — CDN image download latency
- `ocr_triton_batch_size` — Actual batch sizes sent to Triton
- `ocr_phash_cache_hits_total` / `ocr_phash_cache_misses_total` — Cache hit rate
- `gemini_retries_total{reason}` — Retry counts by reason (rate_limit, timeout, etc.)
- `gemini_tokens_total{type}` — Cumulative tokens (prompt, response)
- `stale_jobs_recovered_total` — Jobs reclaimed by sweeper

## Docker Image Convention

All Python services (`init`, `api`, `worker`) share the same Docker image tag:
- **Local dev**: `IMAGE` unset → builds from `./Dockerfile` as `invoice-ocr:local`
- **Deploy**: `IMAGE=ghcr.io/owner/invoice-ocr:sha-abc1234 docker compose up -d` → pulls pinned SHA

This ensures atomic deploys across all services.

## Debugging Tips

### Queue stuck / no jobs processing
```bash
# Check queue depth
docker compose exec redis redis-cli LLEN ocr:queue

# Check worker logs for startup errors
docker compose logs worker --tail 50

# Verify Triton is ready
curl http://localhost:8001/v2/health/ready
```

### Gemini API errors
```bash
# Check API key is set
docker compose exec worker printenv GEMINI_API_KEY

# Check rate limit metrics
curl http://localhost:9102/metrics | grep gemini_retries_total

# Check token bucket state in Redis
docker compose exec redis redis-cli GET rate_limit:tokens
```

### Database connection failures
```bash
# Verify Postgres is healthy
docker compose exec postgres pg_isready -U invoice -d invoice_ocr

# Check connection string
docker compose exec api printenv POSTGRES_DSN

# Inspect job records
docker compose exec postgres psql -U invoice -d invoice_ocr -c "SELECT job_id, status, error_code FROM jobs ORDER BY created_at DESC LIMIT 10;"
```

### Worker crashes or OOM
```bash
# Check container resource usage
docker stats

# Reduce concurrency
# In .env: WORKER_CONCURRENCY=2
docker compose up -d worker
```
