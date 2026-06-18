# Invoice OCR System — Complete Architecture Design

**Version**: 4.0.0  
**Last Updated**: 2026-06-04  
**Status**: Production-ready  
**Authors**: Development Team

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [System Overview](#2-system-overview)
3. [Architecture Principles](#3-architecture-principles)
4. [Component Architecture](#4-component-architecture)
5. [Data Flow & State Machine](#5-data-flow--state-machine)
6. [Storage Layer](#6-storage-layer)
7. [Pipeline Components](#7-pipeline-components)
8. [Concurrency & Scaling Model](#8-concurrency--scaling-model)
9. [Error Handling & Retry Strategy](#9-error-handling--retry-strategy)
10. [Observability & Monitoring](#10-observability--monitoring)
11. [Performance Characteristics](#11-performance-characteristics)
12. [Security & Compliance](#12-security--compliance)
13. [Deployment Architecture](#13-deployment-architecture)

---

## 1. Executive Summary

### Purpose
High-throughput receipt OCR system for extracting structured invoice data from receipt images at scale.

### Key Metrics
- **Throughput**: 10,000 receipts/day steady-state, 30,000/day burst capacity
- **Latency**: ~3 seconds p50 end-to-end (submit → result available)
- **Concurrency**: 16 in-flight jobs per host (4 processes × 4 async tasks)
- **Availability**: 99.9% target (3-nines)

### Technology Stack
- **Detection**: YOLOv11n on NVIDIA Triton Inference Server (GPU-accelerated, dynamic batching)
- **Extraction**: Google Gemini 3.1 Flash Lite (structured JSON schema enforcement)
- **Queue**: Redis LIST (fire-and-forget, BRPOP/LPUSH)
- **Database**: PostgreSQL 16 (asyncpg connection pooling)
- **API**: FastAPI + Uvicorn (async Python)
- **Monitoring**: Prometheus + Grafana
- **CDN**: External image hosting (img-campaign.gotit.vn)

---

## 2. System Overview

### High-Level Data Flow

```
┌─────────┐                                                     ┌──────────────┐
│ Client  │────POST /v1/receipts (image_url)──────────────────>│   FastAPI    │
│         │<───HTTP 202 Accepted (job_id)──────────────────────│     API      │
└─────────┘                                                     └──────┬───────┘
     │                                                                 │
     │                                                                 │
     │                                                          1. Create job
     │                                                          2. Enqueue to Redis
     │                                                                 │
     │                                                          ┌──────▼──────┐
     │                                              ┌──────────>│ PostgreSQL  │
     │                                              │           │   (jobs)    │
     │                                              │           └─────────────┘
     │                                              │
     │                                         Store result      ┌─────────────┐
     │                                              └────────────│    Redis    │
     │                                                           │   (queue)   │
     │                                                           └──────┬──────┘
     │                                                                  │
     │                                                                  │ Pop job
     │                                                                  │
     │                                                           ┌──────▼──────┐
     │                                                           │   Worker    │
     │                                                           │  (4 procs × │
     │                                                           │   4 tasks)  │
     │                                                           └──────┬──────┘
     │                                                                  │
     │                                              ┌───────────────────┼──────────────────┐
     │                                              │                   │                  │
     │                                        Download            Detect (YOLO)      Extract (Gemini)
     │                                              │                   │                  │
     │                                              ▼                   ▼                  ▼
     │                                      ┌──────────────┐    ┌─────────────┐   ┌──────────────┐
     │                                      │ External CDN │    │   Triton    │   │    Gemini    │
     │                                      │   (images)   │    │  (YOLOv11n) │   │  Flash Lite  │
     │                                      └──────────────┘    └─────────────┘   └──────────────┘
     │
     │
     └────GET /v1/receipts/{job_id}──────────────>│ FastAPI API │
          <───HTTP 200 (result JSON)──────────────│ (read from  │
                                                   │  Postgres)  │
```

### Architecture Layers

**Layer 1: Ingress (API)**
- FastAPI async web server
- Domain validation, backpressure checks
- Fire-and-forget: never downloads images

**Layer 2: Queue (Redis)**
- Simple LIST-based queue (`ocr:queue`)
- BRPOP (blocking pop) with 5s timeout
- Decouples fast ingress from slow processing

**Layer 3: Processing (Workers)**
- 4 replicas (docker-compose) per host
- Each replica: 4 async tasks = 16 concurrent jobs
- Downloads images, runs pipeline, persists results

**Layer 4: ML Inference**
- Triton: GPU-accelerated YOLO detection with dynamic batching
- Gemini: Cloud API for structured field extraction

**Layer 5: Storage**
- PostgreSQL: job state + results (JSONB column)
- Redis: pHash cache (24h TTL), rate limit state, requeue counters

---

## 3. Architecture Principles

### 3.1 Fire-and-Forget Pattern

**Design Goal**: API responds instantly (< 50ms) without waiting for processing.

**Implementation**:
1. API validates URL domain only (no download)
2. Creates job record in Postgres (`status: PENDING`)
3. Enqueues message to Redis (`{job_id, image_url}`)
4. Returns HTTP 202 immediately
5. Worker processes asynchronously in background

**Why**: Separates fast ingress from slow ML inference (3s+ per job).

### 3.2 Status-Mirroring HTTP Codes

**Design Goal**: Job status maps directly to HTTP status codes for polling clients.

**Mapping**:
- `200 OK` → `SUCCEEDED` (bare InvoiceResult JSON)
- `202 Accepted` → `PENDING` or `PROCESSING` (envelope with status field)
- `422 Unprocessable Entity` → `FAILED_PERMANENT` (client error, don't retry)
- `503 Service Unavailable` → `FAILED_TRANSIENT` (retry later)
- `404 Not Found` → job_id unknown

**Why**: Clients can use HTTP semantics for retry logic without parsing payloads.

### 3.3 Never Sleep in Async (I3 Invariant)

**Design Goal**: All blocking waits must be `await`-able to preserve event loop responsiveness.

**Implementation**:
- Token bucket rate limiting: `acquire()` returns immediately (bool)
- Rate-limited jobs: yield back to queue (no busy-wait)
- Sweeper: uses `asyncio.wait_for(shutdown.wait(), timeout=60s)` not `time.sleep(60)`

**Why**: Prevents thread-pool exhaustion and maintains high concurrency.

### 3.4 Postprocessor Always Runs (I5 Invariant)

**Design Goal**: Cache stores RAW extraction, postprocessing always runs fresh.

**Implementation**:
- pHash cache key: `ocr:phash:{hash}:psv:{version}`
- Cache stores pre-postprocessed InvoiceResult
- Every result (cache hit or miss) flows through postprocessor

**Why**: Allows whitelist updates without cache invalidation, PSV in key prevents schema drift.

### 3.5 Bounded Requeue (I8 Invariant)

**Design Goal**: Prevent infinite retry loops on pathological jobs.

**Implementation**:
- Redis HINCRBY counter: `ocr:requeue:{job_id}` (1h TTL)
- Threshold: `REQUEUE_MAX=3` (configurable)
- Exceeded counter → mark `FAILED_PERMANENT` with error_code `orphan_requeue_cap`

**Why**: Protects queue from poison messages that repeatedly fail.

### 3.6 Single Source of Truth

**All configuration** via environment variables in `src/config/settings.py` (pydantic-settings).

**No module reads `os.environ` directly**. This ensures:
- Type safety (int/float/list parsing)
- Defaults documented in one place
- Testability (mock Settings instance)

---

## 4. Component Architecture

### 4.1 API Service (`src/api/`)

**Entry Point**: `src/api/app.py` → FastAPI application  
**Routes**: `src/api/routes.py`  
**Port**: 8000 (HTTP), 9101 (Prometheus metrics)

#### Endpoints

**POST /v1/receipts**
```python
Request:  {"image_url": "https://img-campaign.gotit.vn/..."}
Response: {"job_id": "uuid", "status": "PENDING", "message": "..."}
Status:   202 (always, unless validation fails)
```

**Validation**:
1. Domain allowlist check (`ALLOWED_IMAGE_DOMAINS`)
2. Backpressure check (queue depth < `BACKPRESSURE_QUEUE_REJECT=500`)

**GET /v1/receipts/{job_id}**
```python
# Status mirroring
200: {"name": "AEON", "date": "2024-08-15", ...}          # SUCCEEDED
202: {"job_id": "...", "status": "PROCESSING"}            # In-flight
422: {"job_id": "...", "error_code": "no_invoice_detected"} # Permanent
503: {"job_id": "...", "error_code": "gemini_exhausted"}    # Transient
404: {"error_code": "job_not_found"}                        # Unknown job_id
```

**GET /healthz**: Always 200 (liveness)  
**GET /readyz**: 200 if Redis + Postgres + Triton all healthy (readiness)

#### Backpressure

**Metric**: `ocr_api_queue_depth` (current Redis LIST length)

**Thresholds**:
- `< 200`: Normal operation
- `200-499`: Warn (log), continue
- `≥ 500`: Reject with HTTP 429 + `Retry-After: 60` header

**Why**: Prevents unbounded queue growth during traffic spikes.

#### Metrics (Port 9101)

- `http_request_duration_seconds{endpoint}` — Histogram
- `ocr_api_queue_depth` — Gauge
- `ocr_api_backpressure_rejects_total` — Counter

### 4.2 Worker Service (`src/worker/`)

**Entry Point**: `src/worker/main.py`  
**Port**: 9102 (Prometheus metrics)  
**Replicas**: 4 (docker-compose)  
**Concurrency**: 4 async tasks per replica = **16 in-flight jobs per host**

#### Worker Architecture

```
┌─────────────────────────────────────────────────────────┐
│              Worker Process (replica 1/4)               │
├─────────────────────────────────────────────────────────┤
│  Main Thread (asyncio event loop)                       │
│                                                          │
│  ┌──────────────────────────────────────────────────┐  │
│  │ Task 1: execute_task_lifecycle()                  │  │
│  │ Task 2: execute_task_lifecycle()                  │  │
│  │ Task 3: execute_task_lifecycle()                  │  │
│  │ Task 4: execute_task_lifecycle()                  │  │
│  └──────────────────────────────────────────────────┘  │
│                                                          │
│  Background Daemons (same event loop):                  │
│  ┌──────────────────────────────────────────────────┐  │
│  │ sweeper_loop()        — every 60s                 │  │
│  │ rate_refresh_loop()   — every 30s                 │  │
│  │ nightly_purge_loop()  — daily at 02:00           │  │
│  └──────────────────────────────────────────────────┘  │
│                                                          │
│  Shared In-Memory State:                                │
│  - TokenBucket (RPS=4.0, burst=8)                       │
│  - WhitelistIndex (frozen at startup)                   │
└─────────────────────────────────────────────────────────┘
```

#### Worker Loop (`src/worker/loop.py`)

**Entry**: `execute_task_lifecycle(job_id, image_url, bucket, index)`

**State Machine**:
```
PENDING → PROCESSING → SUCCEEDED
                    └─> FAILED_PERMANENT
                    └─> FAILED_TRANSIENT
```

**Lifecycle Steps**:
1. **Preflight**: Check if job already terminal (skip if so)
2. **Mark PROCESSING**: Update Postgres status
3. **Download**: Fetch image from CDN via HTTP (`src/storage/http_client.py`)
4. **Preprocess**: Resize to ≤1600px, compute pHash, JPEG encode (`src/pipeline/preprocessor.py`)
5. **Cache Lookup**: Check `ocr:phash:{hash}:psv:{version}` in Redis
6. **Detect** (if cache miss): YOLO via Triton gRPC → crop bounding box (`src/pipeline/detector.py`)
7. **Rate Limit**: Token bucket acquire (yield if empty)
8. **Extract** (if cache miss): Gemini API → structured JSON (`src/pipeline/extractor.py`)
9. **Cache Write**: Store RAW result (pre-postprocess)
10. **Postprocess**: Normalize dates/money, fuzzy match whitelists (`src/pipeline/postprocessor.py`)
11. **Persist**: Write final result to Postgres (`status: SUCCEEDED`)

**Error Handling**: See Section 9.

#### Background Daemons

**Sweeper** (`src/worker/sweeper.py`):
- **Interval**: Every 60s
- **Query**: `status=PROCESSING AND updated_at < now() - 15min`  
           OR `status=PENDING AND created_at < now() - 30min`
- **Action**: Mark `FAILED_TRANSIENT`, error_code `stale_timeout`
- **Why**: Recovers jobs from crashed workers, OOM kills, network partitions

**Rate Refresh** (`src/worker/rate_refresh.py`):
- **Interval**: Every 30s
- **Action**: Refill token bucket from Redis config (`rate_limit:tokens` HASH)
- **Why**: Allows live rate limit tuning without restart

**Nightly Purge** (`src/worker/nightly_purge.py`):
- **Schedule**: Daily at 02:00 local time (one leader worker only)
- **Query**: Terminal states older than `JOB_RETENTION_DAYS=90`
- **Action**: DELETE FROM jobs (keeps DB size bounded)
- **Leader Election**: Only `WORKER_ID == PURGE_WORKER_ID` runs this

#### Metrics (Port 9102)

- `ocr_cdn_download_seconds` — Histogram (CDN latency)
- `ocr_triton_batch_size` — Histogram (actual batch sizes sent to Triton)
- `ocr_phash_cache_hits_total` / `ocr_phash_cache_misses_total` — Counters
- `gemini_retries_total{reason}` — Counter (rate_limit, timeout, server_error)
- `gemini_tokens_total{kind=prompt|candidates}` — Counter (cumulative tokens)
- `stale_jobs_recovered_total{from_status}` — Counter (sweeper activity)
- `inflight_jobs` — Gauge (current in-flight across all tasks)

### 4.3 Init Service (`src/init/`)

**Entry Point**: `src/init/entrypoint.py`  
**Run Mode**: One-shot container (exits after success)

**Responsibilities**:
1. Wait for Postgres readiness (`pg_isready`)
2. Run Alembic migrations (`alembic upgrade head`)
3. Wait for Redis ping
4. Exit with code 0

**Why**: Ensures database schema is current before API/Worker start (Kubernetes init-container pattern).

### 4.4 Triton Inference Server

**Image**: `nvcr.io/nvidia/tritonserver:24.05-py3`  
**Ports**: 8001 (gRPC), 8002 (metrics), 8000 (HTTP)  
**Model Repository**: `./models/` (mounted read-only)

#### Model Configuration

**Model Name**: `yolov11n_receipt`  
**Input**: `(batch, 3, 640, 640)` FP32 RGB  
**Output**: `(batch, 5, anchors)` — [cx, cy, w, h, conf]

**Dynamic Batching**:
- Enabled in `config.pbtxt`
- Max batch size: 8
- Preferred batch sizes: [4, 8]
- Max queue delay: 100μs

**Why Dynamic Batching**:
- Worker processes send gRPC requests independently
- Triton queues concurrent requests and batches them automatically
- GPU throughput increases 3-4× with batch size 8 vs 1

#### Client (`src/pipeline/triton_client.py`)

```python
async def infer_yolo(batch: np.ndarray) -> np.ndarray:
    # gRPC call to triton:8001
    # Returns (batch, 5, anchors)
```

**Error Handling**:
- gRPC connection refused → `TritonUnavailableError` (transient)
- Inference timeout → `TritonUnavailableError` (transient)

---

## 5. Data Flow & State Machine

### 5.1 Job State Transitions

```
                    ┌──────────┐
                    │ PENDING  │  (created by API)
                    └─────┬────┘
                          │
                          │ Worker pops from queue
                          ▼
                    ┌──────────┐
                    │PROCESSING│  (worker marks this)
                    └─────┬────┘
                          │
          ┌───────────────┼───────────────┐
          │               │               │
          ▼               ▼               ▼
    ┌──────────┐   ┌─────────────┐  ┌──────────────────┐
    │SUCCEEDED │   │FAILED_       │  │FAILED_TRANSIENT  │
    │          │   │PERMANENT     │  │(retry possible)  │
    └──────────┘   └──────────────┘  └──────────────────┘
     (terminal)        (terminal)         (terminal)
```

**State Descriptions**:

- **PENDING**: Job created, waiting in Redis queue
- **PROCESSING**: Worker has popped job, actively processing
- **SUCCEEDED**: Pipeline completed successfully, result available
- **FAILED_PERMANENT**: Unrecoverable error (bad image, no invoice detected, schema error)
- **FAILED_TRANSIENT**: Temporary failure (network timeout, Gemini 5xx, rate limit)

**Terminal States**: Once reached, status never changes. Clients stop polling.

### 5.2 End-to-End Flow Example

**Scenario**: Client submits a receipt URL

```
Time  | Component | Action                                      | Status
------+-----------+---------------------------------------------+------------
T+0ms | Client    | POST /v1/receipts {"image_url": "..."}     |
T+5ms | API       | Validate domain: OK                         |
T+8ms | API       | INSERT jobs (job_id, PENDING, image_url)   | PENDING
T+10ms| API       | LPUSH ocr:queue {"job_id": "...", ...}     |
T+12ms| API       | Return 202 {"job_id": "abc-123", ...}      |
------+-----------+---------------------------------------------+------------
T+50ms| Worker    | BRPOP ocr:queue → got {"job_id": "abc-123"}|
T+52ms| Worker    | UPDATE jobs SET status=PROCESSING           | PROCESSING
T+100ms| Worker   | HTTP GET cdn.com/image.jpg → 2.3 MB         |
T+200ms| Worker   | Preprocess + compute pHash                  |
T+210ms| Worker   | Redis GET ocr:phash:{hash} → MISS           |
T+300ms| Worker   | Triton gRPC infer_yolo() → bbox found       |
T+310ms| Worker   | Token bucket acquire() → OK (tokens=7)      |
T+320ms| Worker   | Gemini API call (async, 15s timeout)        |
T+2500ms| Gemini  | Return JSON response (1200 prompt tokens)   |
T+2510ms| Worker  | Validate schema → OK                        |
T+2520ms| Worker  | Redis SETEX ocr:phash:{hash} (cache RAW)    |
T+2530ms| Worker  | Postprocess: normalize dates, fuzzy match   |
T+2540ms| Worker  | UPDATE jobs SET status=SUCCEEDED, result={} | SUCCEEDED
------+-----------+---------------------------------------------+------------
T+2600ms| Client  | GET /v1/receipts/abc-123                    |
T+2605ms| API     | SELECT * FROM jobs WHERE job_id=...         |
T+2610ms| API     | Return 200 {"name": "AEON", "date": ...}    |
```

**Total Latency**: ~2.6 seconds (typical p50)

### 5.3 Cache Hit Flow

**Scenario**: Duplicate image (same pHash)

```
Time  | Component | Action                                      | Status
------+-----------+---------------------------------------------+------------
T+0ms | Worker    | Preprocess → pHash = "abc123def456"         |
T+10ms| Worker    | Redis GET ocr:phash:abc123def456:psv:v3.7   |
T+15ms| Redis     | HIT → return cached InvoiceResult (RAW)     |
T+20ms| Worker    | Skip YOLO + Gemini (cache hit)              |
T+25ms| Worker    | Postprocess (ALWAYS runs, even on cache hit)|
T+35ms| Worker    | UPDATE jobs SET status=SUCCEEDED, result={} | SUCCEEDED
```

**Latency**: ~300ms (10× faster than full pipeline)

**Cache Key Format**: `ocr:phash:{perceptual_hash}:psv:{PROMPT_SEMANTIC_VERSION}`

**Why PSV in Key**: Schema changes or prompt updates invalidate cache automatically.

---

## 6. Storage Layer

### 6.1 PostgreSQL Schema

**Table**: `jobs`

```sql
CREATE TABLE jobs (
    job_id UUID PRIMARY KEY,
    status VARCHAR(20) NOT NULL,  -- PENDING | PROCESSING | SUCCEEDED | FAILED_*
    phash VARCHAR(32),             -- Perceptual hash (computed after preprocessing)
    image_url TEXT NOT NULL,       -- External CDN URL
    result JSONB,                  -- Final InvoiceResult (NULL until SUCCEEDED)
    error_code VARCHAR(50),        -- Error code on failure
    error_message TEXT,            -- Human-readable error message
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_jobs_status ON jobs(status);
CREATE INDEX idx_jobs_created_at ON jobs(created_at);
CREATE INDEX idx_jobs_updated_at ON jobs(updated_at);
```

**Indexes**:
- `(status)`: Sweeper query optimization
- `(created_at)`: Nightly purge query
- `(updated_at)`: Stale job detection

**Migration History**:
- `0001_initial_jobs.py`: Initial schema
- `0002_cdn_url_migration.py`: Renamed `minio_key` → `image_url` (CDN migration)

**Connection Pool**:
- Driver: `asyncpg` (native PostgreSQL async driver, faster than psycopg3)
- Pool size: 2-10 connections per worker process
- Timeout: 30s

### 6.2 Redis Data Structures

**Queue**: `ocr:queue` (LIST)
```
Structure: LIST of JSON strings
Message:   {"job_id": "uuid", "image_url": "https://..."}
TTL:       None (persistent until consumed)
Operations: LPUSH (API), BRPOP timeout=5s (Worker)
```

**pHash Cache**: `ocr:phash:{hash}:psv:{version}` (STRING)
```
Structure: JSON-encoded InvoiceResult (RAW, pre-postprocess)
TTL:       86400s (24 hours)
Why RAW:   Allows postprocessor logic updates without cache invalidation
```

**Requeue Counter**: `ocr:requeue:{job_id}` (HASH)
```
Structure: HASH {"count": "3"}
TTL:       3600s (1 hour)
Purpose:   Bounded retry (REQUEUE_MAX=3)
Operation: HINCRBY ocr:requeue:{job_id} count 1
```

**Rate Limit Config**: `rate_limit:tokens` (HASH)
```
Structure: HASH {"rps": "4.0", "burst": "8"}
TTL:       None
Purpose:   Live rate limit tuning (read by rate_refresh_loop)
```

### 6.3 External CDN

**Domain**: `img-campaign.gotit.vn` (allowlist enforced in API)

**Access Pattern**:
- API: Never downloads (validation only)
- Worker: HTTP GET with 30s timeout, 10 MB max

**Error Handling**:
- 4xx → `PermanentPipelineError("image_download_failed")`
- 5xx → `StorageTransientError` (requeue job)
- Timeout → `StorageTransientError` (requeue job)

**Why External CDN**:
- Images already hosted by client's infrastructure
- No MinIO/S3 operational overhead
- Simplifies deployment (one less service)

---

## 7. Pipeline Components

### 7.1 Preprocessor (`src/pipeline/preprocessor.py`)

**Input**: Raw image bytes (JPEG/PNG/WebP)  
**Output**: `PreprocessedImage(pil: Image, phash: str)`

**Steps**:
1. Decode image (PIL.Image.open)
2. Compute perceptual hash (imagehash.phash, 8-bit = 16 hex chars)
3. Resize if max(width, height) > 1600px (maintain aspect ratio)
4. Convert to RGB (strip alpha channel if present)

**Why pHash**:
- Near-duplicate detection (same photo taken twice)
- Cache key for extraction results
- Collision rate: ~1 in 2^64 for visually distinct images

### 7.2 Detector (`src/pipeline/detector.py`)

**Input**: PIL Image  
**Output**: Cropped PIL Image (bounding box around receipt)

**Algorithm**:
1. Letterbox resize to 640×640 (pad with gray 114)
2. Normalize to [0,1], convert HWC → CHW, cast to FP32
3. Send (1, 3, 640, 640) batch to Triton gRPC
4. Decode output (1, 5, anchors) → [cx, cy, w, h, conf]
5. Pick argmax(conf), reject if < `YOLO_CONFIDENCE_THRESHOLD=0.35`
6. Reverse letterbox transform → image coordinates
7. Expand box by `YOLO_CROP_PAD_PERCENT=0.02` (2% padding)
8. Clamp to image bounds, crop and return

**No NMS, No Top-K** (I10 Invariant):
- Model trained to predict single highest-confidence receipt
- Simplifies decoding logic
- Faster inference (no post-processing)

**Error Cases**:
- No detection above threshold → `PermanentPipelineError("no_invoice_detected")`

### 7.3 Extractor (`src/pipeline/extractor.py`)

**Input**: Cropped PIL Image (receipt area only)  
**Output**: `InvoiceResult` (structured JSON with 10 fields + products array)

**API**: Google Gemini 3.1 Flash Lite  
**SDK**: `google-genai` (native async client)

**Configuration**:
- Model: `gemini-3.1-flash-lite-preview`
- Temperature: 0.0 (deterministic)
- Response format: `application/json` with strict schema enforcement
- Timeout: 15s per call
- System prompt: Loaded from `src/pipeline/prompts/{PROMPT_SEMANTIC_VERSION}.txt`

**Retry Strategy**:
1. ClientError 429 → `RateLimitedLocallyError` (yield back to queue immediately)
2. ServerError or Timeout → Exponential backoff [0.3s, 0.6s, 1.2s]
3. After 3 retries → `GeminiExhaustedError` (mark FAILED_TRANSIENT)
4. ValidationError (schema mismatch) → `PermanentPipelineError("extractor_invalid_json")`

**Schema Enforcement**:
```python
LEGACY_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},           # Store name
        "type": {"type": "string"},           # Store type (supermarket, restaurant, etc.)
        "date": {"type": "string"},           # YYYY-MM-DD
        "time": {"type": "string"},           # HH:MM
        "pos_id": {"type": "string"},         # POS terminal ID
        "receipt_number": {"type": "string"}, # Receipt number
        "cashier": {"type": "string"},        # Cashier ID
        "total_money": {"type": "string"},    # Total amount (string, not number)
        "barcode": {"type": "string"},        # Barcode
        "products": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "product_id": {"type": "string"},
                    "product_name": {"type": "string"},
                    "product_unit_price": {"type": "string"},
                    "product_quantity": {"type": "string"},
                    "product_discount_money": {"type": "string"},
                    "product_total_money": {"type": "string"}
                }
            }
        }
    }
}
```

**Why Strings for Numbers**:
- Preserves original formatting (e.g., "1,356,000" vs 1356000)
- Avoids float precision issues
- Postprocessor normalizes later

**Lazy Initialization**:
- Prompt and Gemini client load on first `extract_invoice()` call (not at import)
- Why: API container imports this module but never calls extract (separation of concerns)

### 7.4 Postprocessor (`src/pipeline/postprocessor.py`)

**Input**: `InvoiceResult` (RAW from Gemini or cache)  
**Output**: `InvoiceResult` (normalized and whitelist-matched)

**Transformations**:

1. **Date Normalization**: DD/MM/YYYY → YYYY-MM-DD
2. **Time Normalization**: 24-hour format (HH:MM)
3. **Money Normalization**: Remove commas, currency symbols → digits only
4. **Unicode Normalization**: NFC form (Vietnamese diacritics)
5. **Fuzzy Whitelist Matching**:
   - Store name: Match against `whitelists/store_names_whitelist.json` (rapidfuzz, score > 80)
   - Product names: Match against `whitelists/product_names_whitelist.json` (per-product, score > 85)

**Whitelist Index** (`src/pipeline/whitelist_index.py`):
- Loaded at worker startup (frozen in-memory)
- Format: JSON arrays of canonical strings
- Algorithm: `rapidfuzz.process.extractOne(query, choices, scorer=fuzz.ratio)`
- No fallback: If no match above threshold, keep original value

**Why Always Postprocess** (even cache hits):
- Whitelist updates take effect without cache invalidation
- Consistent output regardless of cache state
- Cache stores RAW (smaller payload, faster serialization)

---

## 8. Concurrency & Scaling Model

### 8.1 Worker Concurrency

**Per Host**:
- 4 processes (docker-compose replicas: 4)
- 4 async tasks per process (WORKER_CONCURRENCY=4)
- **Total: 16 concurrent jobs**

**Why 4 processes**:
- Python GIL limits single-process CPU parallelism
- 4 processes fully utilize 4-core CPU
- Async tasks handle I/O concurrency (HTTP, gRPC, Postgres)

**Why 4 tasks per process**:
- Most time spent waiting (CDN download, Triton, Gemini)
- 4 tasks keep process busy without overloading event loop
- Empirically optimal for 4-core machine

### 8.2 Horizontal Scaling

**Strategy**: Add more hosts (VMs/containers)

**Linear Scalability**:
- 1 host: 16 jobs/sec → 1,382,400 jobs/day (10k baseline)
- 2 hosts: 32 jobs/sec → 2,764,800 jobs/day
- 3 hosts: 48 jobs/sec → 4,147,200 jobs/day (30k burst target)

**Bottlenecks**:
1. **Gemini Rate Limit**: 4 RPS per worker (configurable via Redis)
2. **PostgreSQL**: Connection pool limited to 10 per worker (40 total @ 4 replicas)
3. **Redis**: Single-instance LIST operations (vertical scaling needed at ~10k jobs/sec)
4. **Triton**: GPU memory (batch size 8 @ 16 concurrent = 128 jobs queued)

**Scaling Limits**:
- **PostgreSQL**: Migrate to read replicas for GET /v1/receipts/{id} (read-only)
- **Redis**: Cluster mode or Redis Streams (partitioned queue)
- **Triton**: Multi-GPU or model parallelism

### 8.3 Rate Limiting (Token Bucket)

**Algorithm**: Token bucket with refill

**Configuration**:
- RPS: 4.0 (tokens per second)
- Burst: 8 (max tokens in bucket)
- Refill interval: 30s (background daemon)

**Implementation**:
```python
class TokenBucket:
    async def acquire() -> bool:
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False  # Don't block, let caller yield
```

**Flow**:
1. Worker calls `bucket.acquire()` before Gemini API call
2. If `False`, worker yields job back to queue (I3: no sleep)
3. Background daemon refills bucket every 30s

**Why Token Bucket**:
- Allows bursts (up to 8 requests instantly)
- Smooths traffic to Gemini API
- Prevents 429 rate limit errors

### 8.4 Database Connection Pooling

**Driver**: `asyncpg` (native PostgreSQL protocol, faster than psycopg3)

**Pool Size**: 2-10 connections per worker process
- Min: 2 (avoid cold-start latency)
- Max: 10 (4 tasks + daemons + headroom)

**Total Connections**: 40 per host (4 processes × 10)

**Why Pooling**:
- Reuses connections (avoids handshake overhead)
- Async: tasks share pool without blocking

---

## 9. Error Handling & Retry Strategy

### 9.1 Error Classification

**Permanent Errors** (don't retry, mark `FAILED_PERMANENT`):

| Error Code | Cause | HTTP Code |
|------------|-------|-----------|
| `no_invoice_detected` | YOLO confidence < 0.35 | 422 |
| `prompt_missing` | Prompt file not found | 422 |
| `gemini_api_key_missing` | GEMINI_API_KEY unset | 422 |
| `gemini_client_error` | Gemini 4xx (bad request) | 422 |
| `extractor_invalid_json` | Schema validation failed | 422 |
| `image_download_failed` | CDN returned 4xx | 422 |
| `image_too_large` | Image > 10 MB | 422 |
| `orphan_requeue_cap` | Requeue counter > 3 | 422 |

**Transient Errors** (retry, mark `FAILED_TRANSIENT` after exhaustion):

| Error Code | Cause | Retry Strategy |
|------------|-------|----------------|
| `cdn_download_failed` | CDN 5xx or timeout | Yield to queue (bounded requeue) |
| `gemini_exhausted` | Gemini 5xx after 3 retries | Backoff [0.3,0.6,1.2]s then yield |
| `rate_limited_locally` | Gemini 429 or token bucket empty | Yield immediately |
| `triton_unavailable` | gRPC connection refused | Yield to queue |
| `database_unavailable` | Postgres connection failed | Yield to queue |
| `stale_timeout` | Job stuck > 15min | Sweeper marks failed |

### 9.2 Retry Flow

**Bounded Requeue** (I8 Invariant):

```
Job fails with transient error
  │
  ├─> HINCRBY ocr:requeue:{job_id} count 1
  │
  ├─> count <= REQUEUE_MAX (3)?
  │     YES: LPUSH ocr:queue (retry)
  │     NO:  _orphan() → FAILED_PERMANENT (orphan_requeue_cap)
  │
  └─> Touch updated_at (keeps sweeper away)
```

**Why Bounded**:
- Prevents infinite loops on pathological jobs
- Protects queue from poison messages
- Cap of 3 allows transient network blips but stops persistent failures

### 9.3 Sweeper Recovery

**Stale Job Detection**:
```sql
SELECT * FROM jobs
WHERE (status = 'PROCESSING' AND updated_at < now() - INTERVAL '15 minutes')
   OR (status = 'PENDING' AND created_at < now() - INTERVAL '30 minutes')
```

**Recovery Action**:
1. Mark `FAILED_TRANSIENT` with error_code `stale_timeout`
2. Sweeper does NOT requeue (client must retry if desired)

**Why Two Thresholds**:
- PROCESSING: Worker may have crashed mid-pipeline (15min = 5× normal latency)
- PENDING: Queue may be stalled or workers down (30min = abnormal wait time)

**Distributed Sweeping**:
- All workers run sweeper loop (every 60s)
- Race condition is safe: `UPDATE jobs SET status=...` is idempotent
- No distributed lock needed (Postgres handles concurrency)

---

## 10. Observability & Monitoring

### 10.1 Metrics Architecture

**Exporters**:
- API: Port 9101 (Prometheus format)
- Worker: Port 9102 (Prometheus format)

**Scrape Targets** (prometheus.yml):
```yaml
scrape_configs:
  - job_name: 'api'
    static_configs:
      - targets: ['api:9101']
  
  - job_name: 'worker'
    static_configs:
      - targets: ['worker-1:9102', 'worker-2:9102', 'worker-3:9102', 'worker-4:9102']
```

### 10.2 Key Metrics

**API Metrics** (port 9101):

```
# Request latency (histogram)
http_request_duration_seconds{endpoint="/v1/receipts",method="POST"}

# Queue backpressure (gauge)
ocr_api_queue_depth

# Rejection count (counter)
ocr_api_backpressure_rejects_total
```

**Worker Metrics** (port 9102):

```
# Pipeline stage latency (histogram)
stage_duration_seconds{stage="download|preprocess|detect|extract|postprocess"}

# Cache performance (counters)
ocr_phash_cache_hits_total
ocr_phash_cache_misses_total
ocr_phash_schema_drift_total

# Triton batching (histogram)
ocr_triton_batch_size

# Gemini usage (counters)
gemini_tokens_total{kind="prompt|candidates"}
gemini_retries_total{attempt="1|2|3|rate_limited"}

# Recovery (counters)
stale_jobs_recovered_total{from_status="PENDING|PROCESSING"}
orphan_jobs_total

# Rate limiting (counter)
rate_limit_yields_total

# In-flight concurrency (gauge)
inflight_jobs
```

### 10.3 Grafana Dashboards

**Dashboard**: `grafana/dashboards/invoice-ocr-v3.json`

**Panels**:
1. **Throughput**: Jobs completed/min (rate of status transitions to terminal)
2. **Latency**: p50, p95, p99 end-to-end (submit → result available)
3. **Queue Depth**: Current pending jobs (backpressure indicator)
4. **Cache Hit Rate**: phash_hits / (phash_hits + phash_misses)
5. **Error Rate**: Failures/min by error_code
6. **Triton Batch Size**: Average batch size (GPU utilization proxy)
7. **Gemini Token Usage**: Cumulative tokens consumed (cost tracking)
8. **Sweeper Activity**: Stale jobs recovered/hour

**Alerts** (Prometheus Alertmanager):
- Queue depth > 500 for 5 min (backpressure)
- Error rate > 10% for 10 min (pipeline degradation)
- No jobs completed in 5 min (worker down)
- Triton batch size < 2 for 10 min (low concurrency)

### 10.4 Structured Logging

**Format**: JSON (python-json-logger)

**Fields**:
```json
{
  "timestamp": "2026-06-04T10:23:45.123Z",
  "level": "INFO",
  "logger": "src.worker.loop",
  "message": "lifecycle_complete",
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "SUCCEEDED",
  "duration_ms": 2543
}
```

**Correlation**: `job_id` attached to all logs via contextvars (thread-local storage)

**Log Levels**:
- DEBUG: Cache hits, token bucket state
- INFO: Job lifecycle events, daemon ticks
- WARNING: Transient errors, retries
- ERROR: Permanent failures, uncaught exceptions
- CRITICAL: Service boot failures

---

## 11. Performance Characteristics

### 11.1 Latency Breakdown (p50)

| Stage | Duration | Percentage | Notes |
|-------|----------|------------|-------|
| API ingress | 10ms | 0.4% | Validate + enqueue |
| Queue wait | 50ms | 2% | BRPOP latency (low queue depth) |
| CDN download | 200ms | 8% | 2.3 MB avg @ 10 MB/s |
| Preprocess | 100ms | 4% | Resize + pHash (CPU-bound) |
| Triton YOLO | 150ms | 6% | gRPC + inference (GPU) |
| Gemini extract | 1800ms | 72% | API call (network + LLM) |
| Postprocess | 50ms | 2% | Normalize + fuzzy match |
| Postgres write | 20ms | 0.8% | JSONB insert |
| **Total** | **~2500ms** | **100%** | **p50 end-to-end** |

**Gemini dominates latency** (72% of total). Cache hits reduce this to ~300ms.

### 11.2 Throughput

**Single Worker (16 concurrent jobs)**:
- 16 jobs / 2.5s = **6.4 jobs/sec**
- 6.4 × 3600 = **23,040 jobs/hour**
- 23,040 × 24 = **552,960 jobs/day**

**Rate Limit Constraint**:
- Gemini: 4 RPS × 16 workers = **64 RPS theoretical max**
- Cache hit rate ~30% → Effective: **~20 RPS sustained**

**Observed**: ~10,000 jobs/day steady-state (≈0.1 jobs/sec, well under limits)

**Burst Capacity**: 30,000 jobs/day with horizontal scaling to 2-3 hosts.

### 11.3 Resource Usage

**Per Worker Process**:
- CPU: 50% avg, 100% peak (preprocess + postprocess)
- Memory: 512 MB (PIL images + asyncpg pool)
- Network: 10 Mbps avg (CDN downloads)

**Triton Server**:
- GPU: NVIDIA T4 (16 GB VRAM)
- Model size: 20 MB (YOLOv11n)
- Batch 8 GPU util: 80%
- Batch 1 GPU util: 30% (dynamic batching critical)

**PostgreSQL**:
- Storage: 100 MB/10k jobs (JSONB compression)
- IOPS: 50 reads/sec, 20 writes/sec
- Connection pool: 40 (10 per worker × 4 replicas)

**Redis**:
- Memory: 50 MB (queue + cache)
- Ops/sec: 100 (queue ops) + 200 (cache ops)

---

## 12. Security & Compliance

### 12.1 Input Validation

**API Layer**:
1. **Domain Allowlist**: `ALLOWED_IMAGE_DOMAINS` (default: `img-campaign.gotit.vn`)
   - Blocks arbitrary URLs (prevents SSRF)
2. **Image Size Limit**: 10 MB max (prevents DoS via large uploads)
3. **Schema Validation**: Pydantic models enforce strict types

**Worker Layer**:
1. **HTTP Timeout**: 30s (prevents hung connections)
2. **Image Decoding**: PIL sandboxed (no arbitrary code execution)

### 12.2 Secrets Management

**Current** (docker-compose):
- `.env` file (mounted into containers)
- Variables: `GEMINI_API_KEY`, `POSTGRES_PASSWORD`

**Production** (AWS):
- AWS Systems Manager (SSM) Parameter Store
- Secrets injected via environment variables at container start
- No secrets in git, Docker images, or logs

### 12.3 Data Privacy

**Personal Data**: Receipts may contain customer names, payment card last-4-digits

**Retention**:
- Job records: 90 days (configurable via `JOB_RETENTION_DAYS`)
- Nightly purge at 02:00 (one leader worker)

**Gemini API**:
- Google's data usage policy: https://ai.google.dev/gemini-api/terms
- Opt out of data retention via API headers (future enhancement)

### 12.4 Network Security

**Internal Services** (docker network):
- Postgres: Port 5432 (internal only)
- Redis: Port 6379 (internal only)
- Triton: Port 8001 (internal only)

**External Endpoints**:
- API: Port 8000 (public, rate-limited)
- Metrics: Ports 9101, 9102 (internal monitoring network only)

**TLS**:
- Gemini API: HTTPS (enforced by SDK)
- CDN downloads: HTTPS (enforced by URL validation)
- Internal services: Plain text (trusted network)

---

## 13. Deployment Architecture

### 13.1 Docker Compose (Current)

**Services**:
```yaml
services:
  init:        # One-shot: Alembic migrations
  api:         # 1 replica
  worker:      # 4 replicas
  triton:      # 1 replica (GPU)
  postgres:    # 1 replica
  redis:       # 1 replica
  prometheus:  # 1 replica
  grafana:     # 1 replica
```

**Image Strategy**:
- Local dev: `IMAGE` unset → builds from `./Dockerfile` as `invoice-ocr:local`
- Deploy: `IMAGE=ghcr.io/owner/invoice-ocr:sha-abc1234 docker compose up -d`

**Shared Image Tag**:
- `init`, `api`, `worker` use same Docker image
- Entrypoint differs: `["python", "-m", "src.init.entrypoint"]` vs `["uvicorn", ...]`

### 13.2 Kubernetes (Future)

**Deployment Topology**:

```
┌─────────────────────────────────────────────────────────────┐
│                      Kubernetes Cluster                      │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌────────────────────────────────────────────────────┐    │
│  │ Namespace: invoice-ocr-prod                         │    │
│  │                                                      │    │
│  │  ┌──────────────────┐      ┌───────────────────┐  │    │
│  │  │ Deployment: api  │      │ Deployment: worker│  │    │
│  │  │ Replicas: 3      │      │ Replicas: 4       │  │    │
│  │  │ Resources:       │      │ Resources:        │  │    │
│  │  │   CPU: 500m      │      │   CPU: 2000m      │  │    │
│  │  │   Mem: 512Mi     │      │   Mem: 2Gi        │  │    │
│  │  └────────┬─────────┘      └─────────┬─────────┘  │    │
│  │           │                           │             │    │
│  │           │                           │             │    │
│  │           ▼                           ▼             │    │
│  │  ┌──────────────────┐      ┌───────────────────┐  │    │
│  │  │ Service: api     │      │ StatefulSet:      │  │    │
│  │  │ Type: LoadBalancer│     │   triton          │  │    │
│  │  │ Port: 8000       │      │ GPU: 1× T4        │  │    │
│  │  └──────────────────┘      │ PVC: models (RO)  │  │    │
│  │                              └───────────────────┘  │    │
│  │                                                      │    │
│  │  ┌──────────────────┐      ┌───────────────────┐  │    │
│  │  │ StatefulSet:     │      │ StatefulSet:      │  │    │
│  │  │   postgres       │      │   redis           │  │    │
│  │  │ Replicas: 1      │      │ Replicas: 1       │  │    │
│  │  │ PVC: 100Gi       │      │ PVC: 10Gi         │  │    │
│  │  └──────────────────┘      └───────────────────┘  │    │
│  │                                                      │    │
│  │  ┌─────────────────────────────────────────────┐   │    │
│  │  │ Job: init (pre-install hook)                 │   │    │
│  │  │ Runs: Alembic upgrade head                   │   │    │
│  │  └─────────────────────────────────────────────┘   │    │
│  └────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
```

**ConfigMaps**:
- `invoice-ocr-config`: Non-secret env vars (`REDIS_URL`, `TRITON_HOST`, etc.)

**Secrets**:
- `invoice-ocr-secrets`: `GEMINI_API_KEY`, `POSTGRES_PASSWORD`

**Volumes**:
- `models`: Triton model repository (ReadOnlyMany PVC)
- `whitelists`: Store/product name whitelists (ConfigMap)
- `prompts`: Gemini prompt templates (ConfigMap)

**Ingress**:
- NGINX Ingress Controller
- TLS termination (cert-manager)
- Path: `/` → Service `api:8000`

### 13.3 AWS Infrastructure (Removed)

**Previous Setup** (now deleted):
- EC2 VPS instances (provision via `scripts/aws/provision-vps.sh`)
- SSM Parameter Store for secrets
- SSH-based deployment (`ops/deploy-here.sh`)
- GitHub Actions workflows (`.github/workflows/deploy-*.yml`)

**Why Removed**:
- Moved to containerized deployment (Docker Compose / K8s)
- Infrastructure as Code (IaC) not maintained
- Operational overhead of VPS management

**If Restoring AWS**:
```bash
# View deleted scripts
git show 5688529^:scripts/aws/provision-vps.sh
git show 5688529^:ops/deploy-here.sh
```

### 13.4 CI/CD Pipeline (Removed)

**Previous Workflows** (now deleted):
- `.github/workflows/build-push.yml`: Build Docker image, push to ghcr.io
- `.github/workflows/deploy-staging.yml`: Deploy to staging environment
- `.github/workflows/deploy-prod.yml`: Deploy to production
- `.github/workflows/fast-checks.yml`: Lint, type check, unit tests
- `.github/workflows/full-eval.yml`: Integration tests, performance benchmarks

**Current State**: Manual deployment via `docker compose up -d`

**Future**: Helm chart deployment via ArgoCD or Flux (GitOps)

---

## Appendix A: Configuration Reference

### Environment Variables

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| **API** ||||
| `API_HOST` | string | `0.0.0.0` | API bind address |
| `API_PORT` | int | `8000` | API HTTP port |
| `API_MAX_IMAGE_BYTES` | int | `10485760` | Max upload size (10 MB) |
| `API_METRICS_PORT` | int | `9101` | Prometheus metrics port |
| **Image Download** ||||
| `IMAGE_DOWNLOAD_TIMEOUT_SECONDS` | int | `30` | CDN download timeout |
| `IMAGE_DOWNLOAD_MAX_BYTES` | int | `10485760` | Max image size (10 MB) |
| `ALLOWED_IMAGE_DOMAINS` | list[str] | `["img-campaign.gotit.vn"]` | Domain allowlist |
| **Worker** ||||
| `WORKER_METRICS_PORT` | int | `9102` | Prometheus metrics port |
| `WORKER_CONCURRENCY` | int | `4` | Async tasks per process |
| `WORKER_ID` | string | `worker-1` | Unique worker identifier |
| `PURGE_WORKER_ID` | string | `worker-1` | Leader for nightly purge |
| **Redis** ||||
| `REDIS_URL` | string | `redis://redis:6379` | Connection URL |
| `REDIS_QUEUE_KEY` | string | `ocr:queue` | Queue LIST key |
| `REDIS_PHASH_TTL_SECONDS` | int | `86400` | Cache TTL (24h) |
| `REDIS_REQUEUE_TTL_SECONDS` | int | `3600` | Requeue counter TTL (1h) |
| **Postgres** ||||
| `POSTGRES_DSN` | string | `postgresql+asyncpg://...` | Connection string |
| `JOB_RETENTION_DAYS` | int | `90` | Purge threshold |
| **Triton / YOLO** ||||
| `TRITON_HOST` | string | `triton:8001` | gRPC endpoint |
| `YOLO_MODEL_NAME` | string | `yolov11n_receipt` | Model name |
| `YOLO_CONFIDENCE_THRESHOLD` | float | `0.35` | Detection threshold |
| `YOLO_CROP_PAD_PERCENT` | float | `0.02` | Bounding box padding |
| **Preprocess** ||||
| `MAX_IMAGE_DIMENSION` | int | `1600` | Max width/height |
| `JPEG_QUALITY` | int | `85` | Encode quality |
| **Gemini** ||||
| `GEMINI_API_KEY` | string | *(required)* | Google API key |
| `GEMINI_MODEL` | string | `gemini-3.1-flash-lite-preview` | Model name |
| `GEMINI_TIMEOUT_SECONDS` | int | `15` | Per-call timeout |
| `GEMINI_BACKOFFS_SECONDS` | list[float] | `[0.3, 0.6, 1.2]` | Retry backoffs |
| **Prompt** ||||
| `PROMPT_SEMANTIC_VERSION` | string | `v3.7` | Prompt version (cache key) |
| **Rate Limiting** ||||
| `TOKEN_BUCKET_RPS` | float | `4.0` | Tokens per second |
| `TOKEN_BUCKET_BURST` | int | `8` | Max tokens |
| `RATE_LIMIT_REFRESH_INTERVAL` | int | `30` | Refill interval (sec) |
| **Whitelists** ||||
| `WHITELIST_DIR` | string | `/app/whitelists` | Whitelist JSON directory |
| **Sweeper** ||||
| `SWEEP_INTERVAL_SECONDS` | int | `60` | Sweeper tick interval |
| `STALE_PROCESSING_MINUTES` | int | `15` | PROCESSING timeout |
| `STALE_PENDING_MINUTES` | int | `30` | PENDING timeout |
| `REQUEUE_MAX` | int | `3` | Bounded requeue cap |
| **Backpressure** ||||
| `BACKPRESSURE_QUEUE_WARN` | int | `200` | Warning threshold |
| `BACKPRESSURE_QUEUE_REJECT` | int | `500` | Rejection threshold (429) |
| **Logging** ||||
| `LOG_LEVEL` | string | `INFO` | DEBUG/INFO/WARNING/ERROR |

---

## Appendix B: Glossary

- **Fire-and-forget**: API returns immediately without waiting for processing
- **pHash**: Perceptual hash for image near-duplicate detection
- **PSV**: Prompt Semantic Version (cache key component)
- **Terminal state**: Job status that never changes (SUCCEEDED, FAILED_*)
- **Token bucket**: Rate limiting algorithm with burst capacity
- **Dynamic batching**: Triton queues concurrent requests and batches them for GPU efficiency
- **Sweeper**: Background daemon that recovers stale jobs
- **Bounded requeue**: Retry limit (REQUEUE_MAX) to prevent infinite loops
- **Status mirroring**: Job status maps to HTTP status codes (200=success, 422=permanent, 503=transient)

---

## Document Change History

| Date | Version | Author | Changes |
|------|---------|--------|---------|
| 2026-06-04 | 4.0.0 | Development Team | Complete architecture rewrite based on actual codebase |
| 2025-12-20 | 3.0.0 | Previous Team | CDN migration (removed MinIO) |
| 2025-10-15 | 2.0.0 | Previous Team | Added Triton dynamic batching |
| 2025-08-01 | 1.0.0 | Original Team | Initial architecture draft |

---

**End of Document**
