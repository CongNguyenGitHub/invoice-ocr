# Invoice OCR System — Architecture (Final, Consolidated)
**Capacity Target:** 10,000 invoices/day steady; rush-hour bursts up to 30,000/day
**Pattern:** Synchronous Producer-Consumer over Redis LIST+BLPOP RPC with 504 + poll fallback, Triton-batched YOLO microservice, multi-concurrent asyncio workers using the native `google-genai` SDK for Gemini, prompt-driven store classification, raw-extraction pHash cache.
**Image Type:** Mobile phone photos of Vietnamese retail receipts across 8 chains (aeon, bigc, coopmart, coopxtra, emart, lotte, satra, bhx_2024).

> Authoritative document. Must be read together with `task_v3_final.md`. The two documents are fully reconciled — identical names, thresholds, wire shapes, and error paths.

---

## Table of Contents
1. System Overview
2. Design Decisions Log (canonical)
3. Capacity & Traffic Model
4. Component Architecture
5. AI Pipeline (Accuracy Contract)
6. Data Flow & Sequence
7. Triton Dynamic Batching
8. Idempotency Cache (pHash, raw, PSV-versioned)
9. Postprocess & Whitelists (rapidfuzz + bucket prefilter + hot reload)
10. Concurrency, Rate Limiting, Backpressure
11. Schema Evolution (Strangler Pattern)
12. Infrastructure & Deployment
13. Observability & Metrics
14. Fault Tolerance & Recovery Matrix
15. Security Boundaries
16. Configuration Reference
17. Appendix A — Things Deliberately Not Done
18. Appendix B — SYSTEM_PROMPT versioning

---

## 1. System Overview

### 1.1 Purpose
A stateless invoice OCR pipeline. Accepts mobile-phone photos of Vietnamese retail receipts on `POST /v1/receipts` and returns a validated `InvoiceResult` JSON (all string fields, missing → `""`) either synchronously within 60 s, or — on timeout — 504 with a `job_id` the client polls at `GET /v1/receipts/{job_id}`.

### 1.2 Architecture Diagram
```
┌──────────────────────────────────────────────────────────────────────────┐
│ CLIENT                                                                   │
│ POST /v1/receipts (image) ─────────────────── ◄── InvoiceResult (200)   │
│   └─ on 504 → GET /v1/receipts/{id} polling until terminal              │
│ GET /healthz  GET /readyz                                                │
└──────────────────────────────┬───────────────────────────────────────────┘
                               │ port 8000 (external)
┌──────────────────────────────▼───────────────────────────────────────────┐
│ INGRESS — FastAPI                                                        │
│ POST /v1/receipts  → backpressure → upload → DB insert → LPUSH → BLPOP  │
│ GET  /v1/receipts/{id} → PG projection                                   │
│ GET  /healthz  GET /readyz                                               │
│ Internal :9101 prometheus                                                │
└──────────────────────────────┬───────────────────────────────────────────┘
                               │
       ┌──────────┬────────────┼────────────────┐
       ▼          ▼            ▼                ▼
   ┌─────────┐ ┌─────────┐ ┌──────────────┐  ┌──────────────────────────────┐
   │ MinIO   │ │Postgres │ │ Redis        │  │ Triton (gRPC :8001)          │
   │ blobs   │ │ jobs    │ │ queue +      │  │ yolov11n_receipt             │
   │         │ │         │ │ result LIST +│  │ max_batch_size=8             │
   │         │ │         │ │ phash$ +     │  │ max_queue_delay_us=50000     │
   │         │ │         │ │ requeue$     │  │ preferred_batch_size=[4,8]   │
   └─────────┘ └─────────┘ └──────┬───────┘  └──────────────────────────────┘
                                  │ BRPOP "ocr:queue"             ▲
              ┌─────────┬─────────┼──────┬─────────┐              │
              ▼         ▼         ▼      ▼         ▼              │ gRPC
          ┌────────┐┌────────┐┌────────┐┌────────┐                │
          │Worker 1││Worker 2││Worker 3││Worker 4│ ───────────────┘
          │ 4 async││ 4 async││ 4 async││ 4 async│  Prom :9102
          └────────┘└────────┘└────────┘└────────┘   (16 concurrent jobs)
                                │
                                │ HTTPS (google-genai async client)
                                ▼
                    ┌─────────────────────────────────┐
                    │ Gemini 3.1 Flash Lite (preview) │
                    │ via native google-genai SDK     │
                    │ (response_schema strict)        │
                    └─────────────────────────────────┘
```

### 1.3 Rush-hour optimization summary
| Lever | Choice | Why |
|---|---|---|
| YOLO | **Separate Triton microservice, dynamic batching 4–8, 50 ms queue delay** | Coalesces bursts; workers stay tiny, CPU not pinned per worker |
| Worker concurrency | **4 processes × 4 async tasks = 16 in-flight jobs/host** | Gemini is I/O-bound; linear scaling until upstream QPS caps |
| Queue absorption | **Redis list + 429 at `LLEN ≥ 500`, warn at ≥ 200** | ~1 min burst headroom at 16 concurrent workers (Flash Lite drains ~8 jobs/s) |
| RPC channel | **Redis LIST + BLPOP (not pub/sub)** | Survives publish-before-subscribe race deterministically |
| Polling fallback | **504 + `GET /v1/receipts/{id}`** | Survives a single job exceeding 60 s without retry storm |
| Raw-extraction pHash cache | **24 h TTL, PSV-partitioned** | Rush hour sees repeat photos; cache hits return in ~0.5 s |
| Whitelist hot reload | **mtime poll 60 s** | Ops can tune accuracy live mid-burst |
| Metrics | **`api:9101`, `worker:9102`** — separate ports | No port collision on shared host networks |

---

## 2. Design Decisions Log (canonical)

| # | Decision | Rationale |
|---|---|---|
| 1 | `POST /v1/receipts` synchronous with 60 s Redis RPC; on timeout HTTP 504 + `GET /v1/receipts/{id}` poll | One request for the happy path, never fails the client under rush-hour tail latency |
| 2 | **4 worker processes × 4 async concurrency** per host | Rush-hour throughput lever; 16 concurrent Gemini calls/host |
| 3 | **Triton microservice** for YOLO (`max_batch_size=8`, `max_queue_delay_microseconds=50000`, `preferred_batch_size=[4,8]`) | Dynamic batching turns concurrent single-image requests into one efficient kernel |
| 4 | RPC channel is **Redis LIST + BLPOP** (not pub/sub) | Publish cannot be missed even if consumer hasn't reached BLPOP yet; TTL 90 s auto-evicts |
| 5 | `publish_result` BEFORE `delete_file` in SUCCESS path | Cleanup must never block result return |
| 6 | Stale-job sweep covers PROCESSING (>15 min) AND PENDING (>30 min); **sweeper publishes via `_fail`** for any row still within API's 60 s wait | Closes Redis-failure gap; keeps invariant #12 |
| 7 | `SYSTEM_PROMPT` canonical (Appendix B); prompt file path = `prompts/{PROMPT_SEMANTIC_VERSION}.txt` | Bumping PSV forces code-synchronous prompt deploy — no drift |
| 8 | Native `google-genai` SDK with `response_mime_type="application/json"` + `response_schema=LEGACY_JSON_SCHEMA` | Removes markdown-fence / extra-prose failure modes; direct SDK avoids proxy translation layer |
| 9 | Postprocess mandatory on every job (even cache hits) | Whitelist hot reload takes effect immediately |
| 10 | Two whitelists mounted as `:ro`, hot-reloaded every 60 s | Zero-restart accuracy tuning |
| 11 | pHash cache, 24 h TTL, value = **RAW** extraction (pre-postprocess) | Whitelist changes apply on every hit without flushing cache |
| 12 | YOLO crop padding = 2 %; strict YOLO (no fallback to full image) | Wire contract |
| 13 | All-string schema, missing → `""` | Wire contract |
| 14 | No image-quality pre-flight (no Laplacian) | — |
| 15 | No separate store classifier | Store type lives in prompt `type` field |
| 16 | Anti-hallucination prompt rules verbatim | "Do NOT invent…", "Do NOT correct diacritics…", "missing → empty string" |
| 17 | rapidfuzz + `(first3, len//4)` bucket prefilter with ±1 length-bucket drift | Fast fuzzy match with bounded candidate set |
| 18 | Cache key = `ocr:phash:<phash>:psv:<PROMPT_SEMANTIC_VERSION>` | Cosmetic prompt edits don't invalidate cache; behavioral changes do |
| 19 | `genai.Client` singleton (async via `client.aio`); on `ClientError(429)` or empty token bucket → yield-to-queue (bounded) | Never block a worker slot for 15 s |
| 20 | `asyncio.gather(pg_write, redis_publish)` in success and failure paths | Save 5–10 ms TTFB and keep `BLPOP` honest |
| 21 | JPEG quality 85 for LLM payload only | ~40 % egress reduction, no accuracy loss |
| 22 | Local token bucket per **worker process**, refreshed from Redis every 30 s | ~10× fewer Redis ops vs per-call sliding window |
| 23 | HTTP 429 at `LLEN ≥ 500`, soft warn at `LLEN ≥ 200` | Matches 16-concurrent-jobs/host capacity + 60 s SLA horizon |
| 24 | Strangler pattern for json_schema field add/remove | Zero-downtime schema evolution |
| 25 | Module-level `genai.Client` singleton (reused across calls via `client.aio.models`) | Reuse connection pool across calls |
| 26 | Bounded re-queue counter (`REQUEUE_MAX=3`), Redis HASH field `count`, TTL 3600 s | Prevent infinite requeue under sustained throttle |
| 27 | Polling endpoint `GET /v1/receipts/{job_id}` is a **first-class** contract, HTTP status mirrors job status (200 SUCCEEDED / 202 PENDING|PROCESSING / 422 FAILED_PERMANENT / 503 FAILED_TRANSIENT) | Clients branch on status code, not JSON body |
| 28 | Error payload shape (`{job_id, status, error_code, error_message}`) published on every terminal failure, including sweeper-triggered failures | Client gets a structured poll-side response |
| 29 | Orphan job (popped with no Postgres row) → **dead-letter log + metric + drop**, no retry | Prevents silent loss without infinite-loop risk |
| 30 | On cache-hit schema drift: treat `ValidationError` as miss, re-extract, overwrite | Rolls the cache forward during strangler promotions |
| 31 | Wire contract: `submit_receipt` 200 body is the **bare `InvoiceResult`**; `get_receipt` 200 body is the same bare `InvoiceResult`. Non-terminal or failure states return envelope `{job_id, status, error_code?, error_message?}` | Keeps simple clients binary-compatible while giving polling clients a status envelope |
| 32 | `ensure_buckets_exist` runs **only in `init` container**; api/worker fail-fast on missing buckets | Single source of truth, init-gate actually gates |
| 33 | Metrics ports split: `API_METRICS_PORT=9101`, `WORKER_METRICS_PORT=9102` | No `EADDRINUSE` on shared host networks |
| 34 | `ObjectNotFoundError` is classified **permanent** in the worker except-tuple | Missing blob is never retryable — prevents infinite requeue |
| 35 | On `_yield_to_queue`, worker also bumps `jobs.updated_at` so stale-sweeper's 15-min PROCESSING window restarts from the yield moment | Prevents live-but-throttled jobs from being swept as dead |
| 36 | `_fail` is **never-raise** — uses `asyncio.gather(..., return_exceptions=True)` and logs+meters each failed side-effect | Outer worker_loop handler must not recurse if PG or Redis is down mid-failure |
| 37 | `SYSTEM_PROMPT` and `genai.Client` are **lazy-loaded inside `extract_invoice`** (not at import) | API container does not mount `prompts/` and does not have `GEMINI_API_KEY`; lazy load lets api/worker share `src/pipeline/extractor.py` import-safely |
| 38 | `wait_for_result` swallows `redis.ConnectionError` and returns `None` (route then takes the 504+poll branch) | A Redis blip during BLPOP must never 5xx an in-flight job |
| 39 | DB schema is owned by `migrations/` (alembic). Init container runs `alembic upgrade head` then MinIO bucket+lifecycle setup, then exits 0. `api`/`worker` start only after init succeeds | Single source of schema truth; deterministic boot order |
| 40 | `invoices/` bucket has a **7-day MinIO lifecycle expiration** | Floor for orphaned blobs if `_fail`'s idempotency read fails during a PG outage; 7 d ≫ SLA, ≫ realistic op response window |
| 41 | **Native `google-genai` SDK** with model `gemini-3.1-flash-lite-preview` (default); `GEMINI_TIMEOUT_SECONDS=15`; backoff schedule `[0.3, 0.6, 1.2]` | Flash Lite p50 ~2 s halves worker slot occupancy vs prior Flash. Native SDK eliminates a translation proxy and exposes `usage_metadata` directly for cost telemetry |

---

## 3. Capacity & Traffic Model

### 3.1 Throughput
At 16 concurrent in-flight jobs/host, with Gemini Flash Lite p50 = 2 s/miss:

| Metric | Value |
|---|---|
| Steady target | 10 000 invoices/day |
| Sustained throughput per host | 16 / 2 s ≈ **8 jobs/s** |
| Per-host ceiling (no cache, 86 400 s/day) | ~700 k/day |
| Burst peak absorbed | 8 RPS for ~60 s (queue drains in ~60 s at 8 jobs/s) |
| Rush-hour budget (10 k in 2 h) | 1.4 RPS avg, peaks to ~5 RPS — well within one host |
| Backpressure hard threshold | `LLEN ≥ 500` → HTTP 429 |
| Backpressure soft warn | `LLEN ≥ 200` |

### 3.2 Per-request budget (60 s SLA)
```
API side:
  upload + DB insert + LPUSH (gather after upload)   ~0.5 s
  wait_for_result (Redis BLPOP)                      0.1–60 s

Worker (cache HIT path, 10–20 % of traffic):
  download + preprocess                              ~0.4 s
  phash lookup + model_validate                      ~0.005 s
  postprocess                                        ~3 ms
  gather(pg, publish)                                ~30 ms
  TOTAL                                              ~0.5 s

Worker (cache MISS path):
  download + preprocess                              ~0.4 s
  Triton YOLO (batched 4–8)                          ~0.8–1.5 s
  crop + JPEG q85                                    ~0.05 s
  Gemini Flash Lite (timeout 15 s, p50 ~2 s)         1–4 s
  postprocess                                        ~3 ms
  gather(pg, publish)                                ~30 ms
  set pHash                                          ~0.005 s
  TOTAL p50                                          ~3–6 s
  TOTAL p99                                          ~10–18 s
```
Hard ceiling: API returns 504 at 60 s → client polls `GET /v1/receipts/{id}`.

### 3.3 Storage estimates
| Component | Daily | Steady state | Notes |
|---|---|---|---|
| MinIO `invoices/` | ~6 GB | drained on SUCCESS | 600 KB avg |
| MinIO `failed-invoices/` | ~120 MB | 30-day lifecycle | PII-safe window |
| Postgres `jobs` | ~10 MB | ~900 MB (90-day retention) | nightly purge |
| Redis `ocr:queue` | < 1 KB peak | — | drained |
| Redis `ocr:result:{id}` | TTL 90 s | — | auto-evicted |
| Redis `ocr:phash:*` | ~10 k × 1 KB × 24 h | **~240 MB** | rolling |
| Redis `ocr:requeue:{id}` | < 100 keys | tiny | TTL 3600 s |
| Redis `ocr:rate_limit_config` | 1 hash | tiny | operator-controlled |

---

## 4. Component Architecture

### 4.1 API — FastAPI (`src/api/app.py` + `routes.py`)
Endpoints:
- `POST /v1/receipts` — backpressure → upload → PG insert PENDING → LPUSH → `wait_for_result(60 s)`; on None → **HTTP 504** envelope `{job_id, status, message}`
- `GET /v1/receipts/{job_id}` — Postgres projection; HTTP status mirrors job status (see decision #27)
- `GET /healthz` — shallow always-200
- `GET /readyz` — Redis PING + PG `SELECT 1` + MinIO `head_bucket`; 503 on any failure
- Internal `:9101` — Prometheus scrape via `prometheus_client.start_http_server(API_METRICS_PORT)`

Exception handlers (registered in `create_app()`):
- `UnsupportedMediaType` → 415 envelope
- `PayloadTooLarge` → 413 envelope
- `StorageTransientError` → 503 envelope
- `DatabaseUnavailable` → 503 envelope
- `OCRSystemError` → 500 envelope (fallback for any uncaught domain error)

Lifespan (startup, in order):
1. `await pg.init_pool()`
2. `await redis.init()`
3. `await minio.assert_buckets_exist()` (fail-fast, no create — see decision #32)
4. `start_metrics_server(API_METRICS_PORT)`
5. `asyncio.create_task(queue_depth_sampler())` — 5 s cadence, updates `ocr_queue_depth`

Shutdown: cancel sampler → close PG + Redis pools.

### 4.2 Worker (`src/worker/main.py`) — 4 process replicas, 4 async tasks each
Per process:
- `asyncio.run(run_worker())`:
  - init clients; build `TokenBucket`, `WhitelistIndex`
  - `await minio.assert_buckets_exist()` (fail-fast)
  - `start_metrics_server(WORKER_METRICS_PORT)`
  - warm up Triton (one synthetic zero-image inference)
  - spawn daemons:
    - sweeper (60 s)
    - rate-refresh (30 s)
    - whitelist reload (thread, 60 s poll)
    - nightly purge (on `WORKER_ID == PURGE_WORKER_ID`, 02:00 local)
  - spawn `WORKER_CONCURRENCY=4` copies of `worker_loop()`
- Graceful drain on SIGTERM: set `shutdown_event`; `worker_loop`s finish in-flight job; `asyncio.gather` all tasks

### 4.3 Triton microservice (`triton:8001` gRPC)
- Model `yolov11n_receipt`, ONNX
- `max_batch_size=8`, `preferred_batch_size=[4,8]`, `max_queue_delay_microseconds=50000`
- 1 `instance_group`, `KIND_CPU`, 4 vCPU (drop-in replace with `KIND_GPU`)
- Workers send **one image per call**; Triton batches server-side
- No `_yolo_inference_lock` in worker code — Triton owns concurrency (invariant #13)

### 4.4 Storage
- **MinIO**
  - `invoices/` — deleted on SUCCESS; **7-day lifecycle expiration** as a floor for blobs orphaned by a PG outage during `_fail` (decision #40)
  - `failed-invoices/` — 30-day MinIO lifecycle, configured by `init` container
- **Postgres** `jobs`
  - Columns: `job_id PK UUID`, `status`, `phash`, `minio_key`, `failed_minio_key`, `result JSONB`, `error_code`, `error_message`, `created_at`, `updated_at`
  - Index: `(status, updated_at)` partial `WHERE status IN ('PROCESSING','PENDING')` (drives sweeper)
- **Redis**
  - `ocr:queue` LIST — FIFO queue (LPUSH + BRPOP)
  - `ocr:result:{job_id}` LIST (TTL 90 s on first write) — RPC return (BLPOP consumer)
  - `ocr:phash:<phash>:psv:<v>` STRING (TTL 86 400 s) — raw-extraction cache
  - `ocr:requeue:<job_id>` HASH field `count` (TTL 3600 s) — bounded requeue counter
  - `ocr:rate_limit_config` HASH fields `rps`, `burst` — operator-controlled

---

## 5. AI Pipeline (Accuracy Contract)

### 5.1 Preprocessor (`src/pipeline/preprocessor.py`)
Input: raw bytes. Output: `PreprocessResult(pil, phash)` — minimal, two fields only.

Steps:
1. `Image.open(BytesIO(raw)).load()` (catches truncated uploads → `PermanentPipelineError`)
2. `ImageOps.exif_transpose(img)` (full 8-case transform)
3. Convert RGB if needed
4. If `max(img.size) > MAX_IMAGE_DIMENSION`: `img.thumbnail((MAX,MAX), LANCZOS)`
5. `phash = str(imagehash.phash(img))`

**Not done:** blur/brightness check.

### 5.2 pHash cache lookup (raw)
```python
key = phash_cache_key(phash)   # "ocr:phash:<phash>:psv:<PSV>"
cached = await redis.get_phash_cache(phash)
raw: InvoiceResult | None = None
if cached is not None:
    try:
        raw = InvoiceResult.model_validate(cached)
        metrics.phash_hits.inc()
    except ValidationError:
        metrics.phash_schema_drift.inc()    # decision #30
        raw = None                           # fall through to miss
if raw is None:
    metrics.phash_misses.inc()
    # … full Triton + Gemini pipeline …
    await redis.set_phash_cache(phash, raw.model_dump())
```

### 5.3 Detector (`src/pipeline/detector.py`) — Triton client
- `preprocess_for_triton(image)` → FP32 `[3,640,640]` normalized [0,1]
- `tritonclient.grpc.InferenceServerClient` — **module-level singleton** with connection pool
- Filter boxes at `YOLO_CONFIDENCE_THRESHOLD=0.35`
- **argmax** on confidence (no NMS)
- Crop with **2 % pad** in original coords; clamp to bounds
- Empty / below-threshold → `PermanentPipelineError("yolo_no_detection")`
- Returns only the cropped `PIL.Image` — intermediate bbox/confidence are observed via metrics only (no need to surface them up the call stack)

### 5.4 Extractor (`src/pipeline/extractor.py`) — google-genai async client
- Module-level `genai.Client(api_key=settings.GEMINI_API_KEY)` singleton — **lazy-instantiated on first call** (decision #37) so the API container can import the module without `GEMINI_API_KEY`
- `SYSTEM_PROMPT` is also **lazy-loaded** from `prompts/{PSV}.txt` on first call (decision #37); missing file raises `PermanentPipelineError("prompt_file_missing")`
- Re-encode crop to **JPEG quality 85** for LLM payload (`settings.JPEG_QUALITY`); observe `ocr_llm_payload_bytes`
- Request via `client.aio.models.generate_content(...)` with `contents=[SYSTEM_PROMPT_TEXT, types.Part.from_bytes(data=jpeg_bytes, mime_type="image/jpeg")]`
- `config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=LEGACY_JSON_SCHEMA, temperature=0)` — strict structured output
- Per-call timeout `GEMINI_TIMEOUT_SECONDS=15` (Flash Lite p50 ~2 s; 15 s covers tail without burning worker slots)
- **Token bucket**: `await bucket.try_acquire()`; False → `RateLimitedLocallyError` (no token consumed)
- `google.genai.errors.ClientError` with status 429 → `RateLimitedLocallyError("gemini_rate_limited")` → yield-to-queue
- `google.genai.errors.ServerError` (5xx) / `asyncio.TimeoutError` → `asyncio.sleep` exp backoff `[0.3, 0.6, 1.2]`, 3 attempts; final fail → `GeminiExhaustedError`
- `ValidationError` after `InvoiceResult.model_validate(json.loads(resp.text))` → `PermanentPipelineError("extractor_invalid_json")`
- Observe `ocr_extraction_store_type_total{type=<result.type>}` on success
- Observe token usage from `resp.usage_metadata` for cost telemetry

### 5.5 Postprocessor (`src/pipeline/postprocessor.py`) — mandatory
`postprocess(result, whitelist_index)` — **whitelist passed in** (no module-global; avoids API-side build). Order:
1. `type` → strip + lower
2. `name` → `whitelist_index.match_store(…)` (NFC, cutoff 80, fallback 60)
3. `date` → `_normalize_date` (4-format fan-in → `DD/MM/YYYY`)
4. `time` → `_normalize_time` (preserve `HH:MM[:SS]`)
5. `pos_id`, `receipt_number`, `cashier`, `barcode` → NFC + strip
6. `total_money` → `_normalize_money`
7. For each product:
   - `product_name` → `whitelist_index.match_product()` (cutoff 70)
   - prices/discount/total → `_normalize_money`
   - `quantity` → `_normalize_quantity` (float with integer-collapse for `"10.000" → "10"`)

### 5.6 Schema (`src/schemas/invoice.py`)
All fields `str`, missing → `""`. `model_config = ConfigDict(extra="forbid")`.
```
InvoiceResult: name, type, date, time, pos_id, receipt_number, cashier,
               total_money, barcode, products: list[Product]
Product:       product_id, product_name, product_unit_price, product_quantity,
               product_discount_money, product_total_money
```

---

## 6. Data Flow & Sequence

### 6.1 Happy path (cache MISS)
```
Client   FastAPI   MinIO   PG     Redis    Worker(task)   Triton   Gemini
  │POST──►│         │       │       │          │            │        │
  │       ├─LLEN──►(≥500?429)       │          │            │        │
  │       ├─upload─►│       │       │          │            │        │
  │       ├─gather(INSERT PENDING, LPUSH ocr:queue)         │        │
  │       ├─BLPOP ocr:result:{id} (60 s)─────► │            │        │
  │       │         │       │       │◄─BRPOP───┤            │        │
  │       │         │       │◄PROCESSING (+phash)───────────┤        │
  │       │         │◄─GET──────────┤          │            │        │
  │       │         │       │       │◄phash────┤            │        │
  │       │         │       │       │(miss)    │            │        │
  │       │         │       │       │          ├─bucket.acq │        │
  │       │         │       │       │          ├─gRPC──────►│        │
  │       │         │       │       │          │◄─bbox──────│        │
  │       │         │       │       │          ├─crop+q85   │        │
  │       │         │       │       │          ├─generate_content──►│
  │       │         │       │       │          │◄─json──────────────│
  │       │         │       │       │◄SETEX────┤            │        │
  │       │         │       │◄─gather(SUCCEEDED, LPUSH result)       │
  │       │◄─bare InvoiceResult─────┤          │            │        │
  │◄200───┤         │◄delete┤       │          │            │        │
```

### 6.2 Cache HIT
`redis.get_phash_cache` returns raw JSON; `InvoiceResult.model_validate` succeeds. Skip Triton + Gemini + cache write. Postprocess + gather + delete still run. End-to-end ~0.5 s.

### 6.3 YOLO rejection (permanent)
`detect_invoice` raises `PermanentPipelineError("yolo_no_detection")`. Worker catches → `_fail(FAILED_PERMANENT, move_file=True)`:
- `move_to_failed(minio_key)` → new key (idempotent: guarded by `failed_minio_key IS NULL`)
- `asyncio.gather(pg.update_job_status(FAILED_PERMANENT, failed_minio_key=…, error_code="yolo_no_detection", …), redis.publish_result(job_id, ErrorPayload))`
- **No** `delete_file` on failure paths (file was moved, not deleted)
- API surfaces HTTP 422

### 6.4 Gemini rate-limit yield (bounded)
Triggered by `RateLimitedLocallyError`:
```
count = HINCRBY ocr:requeue:{id} count 1
if first bump: EXPIRE key 3600
if count > REQUEUE_MAX (3):
    _fail(FAILED_TRANSIENT, "rate_limit_requeue_exhausted")
else:
    await pg.touch_updated_at(job_id)   # decision #35 — keeps sweeper off
    LPUSH ocr:queue {job_id}
    metrics.rate_limit_yields.inc()
    return   # DO NOT publish_result
```

### 6.5 Orphan job
Worker pops a `job_id` with no Postgres row:
- Log `ERROR event=orphan_job job_id=…`
- `ocr_orphan_jobs_total.inc()`
- Return. **Never requeue.**

### 6.6 Missing-blob failure
`minio.download_file` raises `ObjectNotFoundError`. Worker catches → `_fail(FAILED_PERMANENT, error_code="object_not_found", move_file=False)`. (Permanent — retrying cannot help; operator must re-upload.)

### 6.7 504 + polling
If `BLPOP` returns None at 60 s, API responds HTTP 504 with envelope:
```json
{"job_id":"…","status":"PENDING","message":"still processing — poll GET /v1/receipts/{job_id}"}
```
Worker continues independently. Terminal status lands in Postgres and (if worker is still alive) in the Redis result list — subsequent poll reads from Postgres.

### 6.8 Stale-job sweeper (worker-side)
Every 60 s, sweeper SELECTs jobs matching `(PROCESSING AND updated_at < now()-15 min) OR (PENDING AND created_at < now()-30 min)`. For each row:
1. `UPDATE … status=FAILED_TRANSIENT, error_code='stale_timeout', error_message=…`
2. `publish_result(job_id, ErrorPayload)` — satisfies invariant #12 for any client still BLPOPing (harmless if the list has already expired)
3. `metrics.stale_jobs_recovered.labels(from_status=…).inc()`

---

## 7. Triton Dynamic Batching

### 7.1 `config.pbtxt`
```protobuf
name: "yolov11n_receipt"
platform: "onnxruntime_onnx"
max_batch_size: 8
input  [{ name:"images",  data_type:TYPE_FP32, dims:[3,640,640] }]
output [{ name:"output0", data_type:TYPE_FP32, dims:[-1,6] }]
dynamic_batching {
  preferred_batch_size: [4, 8]
  max_queue_delay_microseconds: 50000
}
instance_group [{ count:1, kind:KIND_CPU }]
```
With 16 concurrent workers/host, Triton routinely forms batches of 4–8 during rush hour → effective p50 per-image YOLO latency ~0.8 s.

### 7.2 Pre/post-processing (worker side)
- `np.float32 / 255.0`, HWC→CHW; remember `(orig_w, orig_h)` for bbox scale-back
- Scale bbox back to orig dims; pad 2 %; clamp to image bounds
- `ocr_triton_batch_size` histogram observed per response via Triton's `response_id`/`metadata` (best-effort; falls back to `1` if unavailable)

---

## 8. Idempotency Cache (pHash)

| Aspect | Spec |
|---|---|
| Hash | `imagehash.phash(image)` after EXIF + resize |
| Key | `ocr:phash:<phash_hex>:psv:<PROMPT_SEMANTIC_VERSION>` |
| TTL | 86 400 s |
| Value | **RAW `InvoiceResult.model_dump_json()`** (pre-postprocess) |
| On miss | Full pipeline; on success: `redis.setex(key, TTL, raw_json)` |
| Versioning | Bump `PROMPT_SEMANTIC_VERSION` ONLY for behavioral prompt / schema changes. Cosmetic edits do not bump. Whitelist version NOT in key (postprocess re-runs on every hit) |
| Schema drift | Cached JSON fails `InvoiceResult.model_validate` → treat as miss → re-extract → overwrite (invariant #5) |
| Storage | ~240 MB Redis steady state |

---

## 9. Postprocess & Whitelists

### 9.1 Files (volume-mounted `:ro`)
- `whitelists/store_names_whitelist.json`
- `whitelists/product_names_whitelist.json`

### 9.2 `WhitelistIndex` (per-worker-process singleton)
One index object, two sub-indexes (`store`, `product`). Internal layout per kind:
```python
_buckets: dict[tuple[str,int], list[tuple[str,str]]]   # (first3, len//4) → [(lower, canonical)]
_all_lower: list[str]                                  # full fallback scan
_canonical_of: dict[str, str]                          # lower → canonical
last_mtime: dict[str, float]
source_path: dict[str, Path]
_lock: threading.Lock
```
API methods are `build()` (classmethod, one-shot at startup), `reload(kind, path)` (atomic swap under `_lock`), `match_store(raw)`, `match_product(raw)`. No redundant `load()` method.

### 9.3 Fuzzy match contract (`rapidfuzz`)
```
1. NFC-normalize + strip; if empty return as-is
2. key = (lower[:3], len(lower)//4)
3. candidates = _buckets[key] + _buckets[(k0, k1-1)] + _buckets[(k0, k1+1)]
   — tolerates ±1 length-bucket drift
4. If no candidates → full_lower fallback scan (store only; product returns NFC raw)
5. best,score = rapidfuzz.process.extractOne(lower, candidates_lower, scorer=WRatio)
6. store: primary 80, fallback 60; product: primary 70, no fallback
7. score >= cutoff → canonical_of[best]; else NFC(raw)
```
| Field | Primary | Fallback |
|---|---|---|
| store_name | 80 | 60 (full scan) |
| product_name | 70 | — (returns NFC raw when below cutoff) |

### 9.4 Hot reload
Daemon **thread** per process, polling mtime every 60 s.
- On change: build new `_buckets/_all_lower/_canonical_of` triple → atomic swap under `_lock` → emit `ocr_whitelist_reload_total{file=<kind>}`
- On parse/IO failure: keep current → `ocr_whitelist_reload_failed_total{file,reason}`
- Thread checks a mirrored `threading.Event` (`shutdown_tevent`) set alongside the asyncio `shutdown_event` in `run_worker()` for clean exit

### 9.5 Coverage metric
`ocr_whitelist_match_total{field=store|product, tier=exact|fuzzy_high|fuzzy_low|miss}` — emitted from `match_store`/`match_product` based on score band and cutoff outcome.

---

## 10. Concurrency, Rate Limiting, Backpressure

### 10.1 Local token bucket (per worker process)
- Parameters: `rps` (float), `burst` (int). Defaults: `TOKEN_BUCKET_RPS=4.0`, `TOKEN_BUCKET_BURST=8`.
- Refresh: 30 s daemon calls `redis.read_rate_limit_config()` → `bucket.reconfigure(rps, burst)` under the bucket's `asyncio.Lock`.
- Operator control (no restart): `redis-cli HSET ocr:rate_limit_config rps 8 burst 16`.
- `try_acquire` is non-blocking; False → caller raises `RateLimitedLocallyError` (no sleep, no token consumed).
- `bucket.available()` drives `ocr_token_bucket_available` gauge (sampled every refresh tick).

### 10.2 Async backoff + yield-to-queue
- `genai.Client.aio` async end-to-end; **no `time.sleep` anywhere in asyncio code** (invariant #3).
- On `google.genai.errors.ServerError` (5xx) / `asyncio.TimeoutError`: `asyncio.sleep(0.3/0.6/1.2)` exp-backoff up to 3 attempts → `GeminiExhaustedError` (FAILED_TRANSIENT).
- On `google.genai.errors.ClientError` with status 429 OR local bucket empty → `RateLimitedLocallyError` → yield-to-queue.

### 10.3 Concurrent SUCCESS / FAILED writes
Success path in `execute_task_lifecycle`:
```python
await asyncio.gather(
    pg.update_job_status(job_id, SUCCEEDED, result=final.model_dump(), phash=pp.phash),
    redis.publish_result(job_id, SuccessPayload(...).model_dump()),
)
try: await asyncio.to_thread(minio.delete_file, minio_key)
except Exception as e: logger.warning("delete_failed", ...)   # never re-raise
```
Failure path — single `_fail` helper (see task §19 Appendix). Idempotency: `move_to_failed` checks `failed_minio_key IS NULL` before copying to prevent double-moves under double-dispatch.

### 10.4 Backpressure
- API checks `LLEN ocr:queue` before LPUSH
- `LLEN ≥ BACKPRESSURE_QUEUE_REJECT` (500) → HTTP 429 + `Retry-After: 5`; `ocr_backpressure_rejections_total`
- `LLEN ≥ BACKPRESSURE_QUEUE_WARN` (200) → soft warn counter `ocr_queue_soft_warn_total`, no 429
- Cache hits drain fast (~0.5 s) → backpressure relaxes naturally

### 10.5 In-flight gauge
`execute_task_lifecycle` wraps its body with `ocr_inflight_jobs.inc()` / `.dec()` (try/finally). Gauge cap = `WORKER_CONCURRENCY × replicas` = 16.

---

## 11. Schema Evolution (Strangler Pattern)

### 11.1 Adding a field
- **Stage A (≥ 1 week observation):** add to Python model with default `""`; Gemini json_schema `strict=false`; `ocr_new_field_present_total{field}` counter incremented in extractor when the field is non-empty.
- **Stage B (≥ 99 % present):** promote to `required`, re-enable `strict=true`; bump `PROMPT_SEMANTIC_VERSION` (forces cache partition change).

### 11.2 Removing a field
- Phase 1: stop reading downstream
- Phase 2: remove from `required`
- Phase 3: remove from `properties` + Python model; bump PSV

### 11.3 Cache-hit schema drift
Strict mode means cached raw JSON may fail `model_validate` after a promotion. Handled by decision #30: `ValidationError` on cache-hit → miss, re-extract, overwrite. Emits `ocr_phash_schema_drift_total`.

---

## 12. Infrastructure & Deployment

### 12.1 Docker Compose services
| Service | Image | CPU | RAM | Ports | Notes |
|---|---|---|---|---|---|
| `init` | python:3.11-slim | 0.1 | 64 M | — | `alembic upgrade head` + bucket create + lifecycle, exits 0. **Sole `ensure_buckets_exist` + `configure_lifecycles` caller.** Entrypoint `python -m src.init.entrypoint` |
| `api` | python:3.11-slim | 0.5 | 512 M | 8000 ext, 9101 int | FastAPI + metrics |
| `worker` | python:3.11-slim | 1.0 | 1 G | 9102 int | `replicas: 4`, 4 async tasks each, whitelists `:ro` |
| `redis` | redis:7-alpine | 0.25 | 256 M | 6379 int | |
| `postgres` | postgres:15-alpine | 0.5 | 512 M | 5432 int | |
| `minio` | minio/minio | 0.5 | 512 M | 9000, 9001 int | |
| `triton` | nvcr.io/nvidia/tritonserver:24.x-py3 | 4.0 | 4 G | 8001 int | mounts `./models:ro` |
| `prometheus` | prom/prometheus | 0.1 | 128 M | 9090 | scrapes `api:9101`, `worker:9102` |
| `grafana` | grafana/grafana | 0.1 | 128 M | 3000 | |

**Total:** ~10.5 vCPU, ~11 GB RAM. Recommended host: c5.4xlarge (16 vCPU, 32 GB).

> Note: `worker.replicas` is controlled by compose; env var `WORKER_NUM_PROCESSES` is **documentation only** (mirrors the replica count) and is not consumed by the worker entrypoint.

### 12.2 Volumes
- `./models` → triton `:ro`
- `./whitelists` → worker `:ro` (`/app/whitelists`)
- `postgres_data`, `minio_data`, `redis_data`

### 12.3 Startup chain
`postgres, minio → init → api, worker(×4)`. `redis`, `triton` independent.

---

## 13. Observability & Metrics

### 13.1 Metrics
All metrics defined in `src/api/metrics.py` (API-scoped) and `src/worker/metrics.py` (worker-scoped); both importable as `metrics`. Each metric below is annotated with its emission site.

| Metric | Type | Labels | Emitted by |
|---|---|---|---|
| `ocr_requests_total` | Counter | `status`: success/pipeline_failed/timeout/storage_error/backpressure | `submit_receipt` |
| `ocr_e2e_latency_seconds` | Histogram | — | `submit_receipt` (try/finally) |
| `ocr_queue_depth` | Gauge | — | `queue_depth_sampler` |
| `ocr_queue_soft_warn_total` | Counter | — | `check_backpressure` |
| `ocr_backpressure_rejections_total` | Counter | — | `check_backpressure` |
| `ocr_stage_duration_seconds` | Histogram | `stage`: download/preprocess/phash_lookup/yolo/gemini/postprocess/publish | `execute_task_lifecycle` stage timers |
| `ocr_phash_hits_total` | Counter | — | cache branch |
| `ocr_phash_misses_total` | Counter | — | cache branch |
| `ocr_phash_schema_drift_total` | Counter | — | cache-hit ValidationError |
| `ocr_yolo_rejection_total` | Counter | — | `detect_invoice` |
| `ocr_triton_batch_size` | Histogram | — | `infer_yolo` post-response (best-effort) |
| `ocr_gemini_retries_total` | Counter | `attempt` | `extract_invoice` retry loop |
| `ocr_gemini_tokens_total` | Counter | `kind`: prompt/output | `extract_invoice` post-response from `resp.usage_metadata` |
| `ocr_rate_limit_yields_total` | Counter | — | `_yield_to_queue` |
| `ocr_requeue_count` | Histogram | — | `_yield_to_queue` |
| `ocr_token_bucket_acquire_total` | Counter | `outcome`: ok/empty | `TokenBucket.try_acquire` |
| `ocr_token_bucket_refresh_total` | Counter | `outcome`: ok/redis_error | `refresh_rate_limit_daemon` |
| `ocr_token_bucket_available` | Gauge | — | `refresh_rate_limit_daemon` (each tick) |
| `ocr_inflight_jobs` | Gauge | — | `execute_task_lifecycle` inc/dec |
| `ocr_whitelist_match_total` | Counter | `field`: store/product, `tier`: exact/fuzzy_high/fuzzy_low/miss | `match_store`/`match_product` |
| `ocr_whitelist_reload_total` | Counter | `file`: store/product | `whitelist_reload_thread` |
| `ocr_whitelist_reload_failed_total` | Counter | `file`, `reason` | `whitelist_reload_thread` |
| `ocr_postprocess_duration_seconds` | Histogram | — | `postprocess` wrapper |
| `ocr_extraction_store_type_total` | Counter | `type` | `extract_invoice` on success |
| `ocr_llm_payload_bytes` | Histogram | — | `extract_invoice` before call |
| `ocr_new_field_present_total` | Counter | `field` | `extract_invoice` post-parse, strangler Stage A only |
| `ocr_stale_jobs_recovered_total` | Counter | `from_status` | `sweep_stale_jobs_daemon` |
| `ocr_orphan_jobs_total` | Counter | — | `_orphan` |
| `ocr_storage_errors_total` | Counter | `service`: minio/postgres/redis | storage client error branches |
| `ocr_fail_side_effect_errors_total` | Counter | `side`: pg_update_failed/publish_failed | `_fail` per-side guard (decision #36) |
| `ocr_wait_redis_drops_total` | Counter | — | `wait_for_result` connection-error branch (decision #38) |

### 13.2 Grafana panels (thresholds aligned to §10.4)
1. Request rate (alert > 5 RPS sustained 5 m)
2. Queue depth (warn > 200 for 30 s, crit > 500 for 60 s)
3. Success rate (warn < 95 % for 5 m, crit < 80 %)
4. Latency p50/p95 (warn p95 > 20 s — Flash Lite cache-miss budget; alert at 45 s)
5. YOLO rejection rate (warn > 10 % for 10 m)
6. Triton batch efficiency (alert if `ocr_triton_batch_size` p50 = 1 at peak)
7. Gemini retry/yield rate (alert > 0.1/s)
8. Stale job recovery (any → alert)
9. Pipeline stage latency p95 (per-stage)
10. pHash hit rate (alert on cliff)
11. Whitelist miss rate (warn > 5 % for 1 h → new chain)
12. Store-type distribution (pie from `ocr_extraction_store_type_total`)
13. Token bucket acquire success rate (warn < 95 %)
14. LLM payload bytes p95 (verify q85 active)

### 13.3 Structured logs
```json
{"ts":"…","level":"INFO","service":"worker","worker_id":"worker-3",
 "job_id":"…","event":"pipeline_stage_complete","stage":"yolo","duration_ms":820}
```

---

## 14. Fault Tolerance & Recovery Matrix

| Failure | Detection | Response | Recovery |
|---|---|---|---|
| MinIO / PG / Redis offline at ingress | connection error | HTTP 503 from `/readyz`; `submit_receipt` 503 via `StorageTransientError` handler | client retries |
| 60 s API timeout (rush-hour tail) | `wait_for_result` None | HTTP 504 envelope | client polls `GET /v1/receipts/{id}` |
| Worker OOM mid-job | stale PROCESSING > 15 min | sweeper → `_fail`-equivalent (FAILED_TRANSIENT + publish) | client poll gets failure |
| Redis fails between LPUSH and BRPOP | stale PENDING > 30 min | sweeper → FAILED_TRANSIENT + publish | client poll gets failure |
| YOLO no detection | `PermanentPipelineError("yolo_no_detection")` | `_fail(FAILED_PERMANENT, move_file=True)` + publish | client re-photographs; 422 |
| MinIO object missing | `ObjectNotFoundError` | `_fail(FAILED_PERMANENT, "object_not_found")` (decision #34) | operator re-upload |
| Gemini 429 (light) | `RateLimitedLocallyError` (`ClientError` status 429) | yield-to-queue (count ≤ 3) + touch `updated_at` | auto-heals |
| Gemini 429 (sustained) | count > 3 | `_fail(FAILED_TRANSIENT, "rate_limit_requeue_exhausted")` | client retries |
| Gemini 5xx / timeout | exp backoff 3 attempts | → `GeminiExhaustedError` → FAILED_TRANSIENT | client retries |
| Gemini invalid JSON (despite strict response_schema) | `ValidationError` | `PermanentPipelineError("extractor_invalid_json")` → FAILED_PERMANENT + 422 | prompt review |
| Vietnamese amount misparse | postprocess `_normalize_money` | auto-handled | — |
| Whitelist file missing at boot | empty index | NFC passthrough; `ocr_whitelist_match_total{tier=miss}=100 %` → alert | redeploy |
| Whitelist hot-reload parse fail | logged + metric | keep current index | fix file, mtime poll picks up |
| pHash cache write fails after SUCCESS | try/except | log warning; result already published | next request misses, re-extracts |
| `delete_file` fails after SUCCESS | try/except | log warning | weekly MinIO lifecycle sweep |
| Triton unavailable | gRPC connection refused | `TritonUnavailableError` (subclass of `TransientPipelineError`) → FAILED_TRANSIENT | ops check container + model mount |
| Triton batch never groups | panel 6 = p50:1 | alert | review `max_queue_delay_microseconds` |
| Postgres offline at SUCCESS write | `gather` raises → caught by outer except → `_fail(FAILED_TRANSIENT,"db_unavailable")` via `DatabaseUnavailable` | Single authoritative outcome; no split-brain | log; next retry reconciles |
| Orphan job (pop with no PG row) | `record is None` | log + `ocr_orphan_jobs_total` + return | manual audit |
| Cache-hit schema drift | `ValidationError` on cached JSON | treat as miss, re-extract, overwrite | auto-heals |

---

## 15. Security Boundaries
- Upload size: `API_MAX_IMAGE_BYTES=10_485_760` enforced by FastAPI dependency (raises `PayloadTooLarge` → 413)
- Content-Type allowlist: `image/jpeg|png|webp` (raises `UnsupportedMediaType` → 415)
- UUID4 job_ids (122-bit, non-enumerable)
- Internal-only ports: 9101 (api metrics), 9102 (worker metrics), 6379, 5432, 9000/9001, 8001
- `failed-invoices/` 30-day MinIO lifecycle
- Gemini API key via env, never logged, never returned
- Models `:ro`; whitelists `:ro`

---

## 16. Configuration Reference (canonical — matches `task_v3_final.md`)

| Variable | Service | Default | Purpose |
|---|---|---|---|
| `API_HOST` | api | `0.0.0.0` | |
| `API_PORT` | api | `8000` | |
| `API_TIMEOUT_SECONDS` | api | `60` | RPC wait + 504 boundary |
| `API_MAX_IMAGE_BYTES` | api | `10_485_760` | 10 MB |
| `API_METRICS_PORT` | api | `9101` | Prometheus scrape |
| `WORKER_METRICS_PORT` | worker | `9102` | Prometheus scrape (distinct from api) |
| `REDIS_URL` | api, worker | `redis://redis:6379` | |
| `REDIS_QUEUE_KEY` | all | `ocr:queue` | |
| `REDIS_RESULT_CHANNEL_FMT` | all | `ocr:result:{job_id}` | LIST key (BLPOP/LPUSH) |
| `REDIS_RESULT_TTL_SECONDS` | api, worker | `90` | |
| `REDIS_PHASH_TTL_SECONDS` | worker | `86400` | |
| `REDIS_REQUEUE_TTL_SECONDS` | worker | `3600` | |
| `POSTGRES_DSN` | all | `postgresql+asyncpg://...` | |
| `MINIO_ENDPOINT` | all | `minio:9000` | |
| `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` | all | — | |
| `MINIO_BUCKET_UPLOADS` | all | `invoices` | |
| `MINIO_BUCKET_FAILED` | worker, init | `failed-invoices` | |
| `TRITON_HOST` | worker | `triton:8001` | |
| `YOLO_MODEL_NAME` | worker | `yolov11n_receipt` | Triton model name |
| `YOLO_CONFIDENCE_THRESHOLD` | worker | `0.35` | |
| `YOLO_CROP_PAD_PERCENT` | worker | `0.02` | |
| `MAX_IMAGE_DIMENSION` | worker | `1600` | |
| `JPEG_QUALITY` | worker | `85` | LLM-only re-encode |
| `GEMINI_API_KEY` | worker | — | Native `google-genai` auth |
| `GEMINI_MODEL` | worker | `gemini-3.1-flash-lite-preview` | If this model id is unavailable in your region, override to `gemini-2.5-flash-lite` |
| `GEMINI_TIMEOUT_SECONDS` | worker | `15` | Per-call Flash Lite ceiling; p50 ~2 s |
| `GEMINI_BACKOFFS_SECONDS` | worker | `[0.3, 0.6, 1.2]` | **JSON list** parsed by pydantic-settings; 3-attempt exp schedule |
| `PROMPT_SEMANTIC_VERSION` | worker | `v3.4` | Also drives prompt file path `prompts/{PSV}.txt` (loaded lazily on first extract call — decision #37) |
| `WORKER_NUM_PROCESSES` | docs only | `4` | Documentation mirror of compose `replicas` (not consumed at runtime) |
| `WORKER_CONCURRENCY` | worker | `4` | Async tasks per process |
| `TOKEN_BUCKET_RPS` | worker | `4.0` | Per-process default |
| `TOKEN_BUCKET_BURST` | worker | `8` | Per-process default |
| `RATE_LIMIT_REFRESH_INTERVAL` | worker | `30` | Seconds |
| `WHITELIST_DIR` | worker | `/app/whitelists` | |
| `WHITELIST_RELOAD_INTERVAL` | worker | `60` | Seconds |
| `SWEEP_INTERVAL_SECONDS` | worker | `60` | |
| `STALE_PROCESSING_MINUTES` | worker | `15` | |
| `STALE_PENDING_MINUTES` | worker | `30` | |
| `REQUEUE_MAX` | worker | `3` | |
| `BACKPRESSURE_QUEUE_WARN` | api | `200` | |
| `BACKPRESSURE_QUEUE_REJECT` | api | `500` | HTTP 429 |
| `WORKER_ID` | worker | docker hostname | Log correlation + purge selector |
| `PURGE_WORKER_ID` | worker | `worker-1` | Nightly 02:00 purge runner |
| `JOB_RETENTION_DAYS` | worker | `90` | |

---

## 17. Appendix A — Things Deliberately Not Done
- Inotify-based whitelist reload (mtime poll suffices at our scale)
- Gemini Files API (native SDK supports it, but q85 is already a cheap win; revisit if egress bill spikes)
- dHash composite (revisit only if false-cache-hit metric fires)
- Multi-region Redis (out of scope at current scale)
- Image-quality pre-flight / Laplacian
- Separate store classifier (prompt does it via `type`)
- Totals reconciliation
- WebSocket push for results (polling endpoint suffices)
- Surfacing bbox / confidence / original_size up the pipeline (only needed for debugging — available via structured logs)

---

## 18. Appendix B — SYSTEM_PROMPT versioning
The canonical prompt text lives at `src/pipeline/prompts/{PROMPT_SEMANTIC_VERSION}.txt`. The extractor loads it lazily on first `extract_invoice` call (decision #37). **PSV bump ⇒ new file + coordinated deploy.** No drift is possible.

| Change type | Bump PSV? |
|---|---|
| Add new store rule | Yes |
| Change schema (`required` fields) | Yes |
| Tighten extraction rule | Yes |
| Reword for clarity, no behavioral change | No |
| Whitespace / formatting | No |
| Add/remove example | No |

---

*End of architecture_v3_final.md. Pair with `task_v3_final.md`.*
