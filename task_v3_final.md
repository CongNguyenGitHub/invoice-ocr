# Invoice OCR — Development Task & Function Specification

> Companion to `architecture_v3_final.md`. Reconciled — names, thresholds,
> endpoints, wire shapes, and error paths match exactly. This is the
> authoritative development backbone: every module, class, function, signature,
> contract, and dependency needed to build the rush-hour-ready system from
> scratch.
>
> **Read order:** `architecture_v3_final.md` → this file → implement.

---

## Table of Contents
1. Module Map
2. Configuration (`src/config`)
3. Schemas (`src/schemas`)
4. Domain — Errors & Constants (`src/domain`)
5. Storage Clients (`src/storage`)
6. Triton Client (`src/pipeline/triton_client.py`)
7. API Layer (`src/api`)
8. Worker Layer (`src/worker`)
9. Pipeline Layer (`src/pipeline`)
10. Utility Classes
11. Background Tasks & Daemons
12. Exception Hierarchy
13. Function Dependency Graph
14. Invariants
15. Test Plan Hooks
16. Development Order (Milestones)
17. Init Container & DB Migrations
18. Logging Configuration
19. Appendix — Example Stubs (`execute_task_lifecycle`, `_fail`)

---

## 1. Module Map

```
src/
├── config/
│   └── settings.py               # pydantic-settings singleton (single key-helper here)
├── schemas/
│   └── invoice.py                # InvoiceResult, Product, JobRecord, ErrorPayload, SuccessPayload
├── domain/
│   ├── errors.py                 # Exception hierarchy
│   └── constants.py              # JobStatus enum, Redis key formats
├── storage/
│   ├── minio_client.py           # ensure_buckets_exist (init only) + assert_buckets_exist
│   ├── postgres_client.py
│   └── redis_client.py
├── api/
│   ├── app.py                    # FastAPI factory + lifespan + exception handlers
│   ├── routes.py                 # POST /v1/receipts, GET /v1/receipts/{id}, /healthz, /readyz
│   ├── backpressure.py
│   └── metrics.py                # API-side Prom metrics + queue-depth sampler
├── worker/
│   ├── main.py                   # Entrypoint, signal handlers, singletons
│   ├── loop.py                   # worker_loop, execute_task_lifecycle, _fail, _yield_to_queue, _orphan
│   ├── sweeper.py
│   ├── rate_refresh.py
│   ├── whitelist_reload.py       # thread daemon
│   ├── nightly_purge.py
│   └── metrics.py                # Worker-side Prom metrics
├── pipeline/
│   ├── preprocessor.py
│   ├── detector.py               # Triton-backed detect_invoice
│   ├── triton_client.py          # singleton InferenceServerClient + infer_yolo
│   ├── extractor.py              # google-genai async client; loads prompts/{PSV}.txt
│   ├── postprocessor.py          # postprocess(result, whitelist_index)
│   ├── whitelist_index.py        # rapidfuzz bucket prefilter
│   ├── prompts/
│   │   └── v3.4.txt              # frozen SYSTEM_PROMPT, file name == PSV
│   └── json_schema.py            # LEGACY_JSON_SCHEMA for Gemini strict mode
└── utils/
    └── token_bucket.py
```

`whitelists/` lives OUTSIDE `src/` and is mounted `:ro` at `/app/whitelists`.

---

## 2. Configuration (`src/config/settings.py`)

### Class `Settings(BaseSettings)`

| Field | Type | Default | Notes |
|---|---|---|---|
| `API_HOST` | str | `0.0.0.0` | |
| `API_PORT` | int | `8000` | |
| `API_TIMEOUT_SECONDS` | int | `60` | RPC wait + 504 boundary |
| `API_MAX_IMAGE_BYTES` | int | `10_485_760` | 10 MB |
| `API_METRICS_PORT` | int | `9101` | Prom scrape (api) |
| `WORKER_METRICS_PORT` | int | `9102` | Prom scrape (worker) — **must differ from api** |
| `REDIS_URL` | str | — | |
| `REDIS_QUEUE_KEY` | str | `ocr:queue` | |
| `REDIS_RESULT_CHANNEL_FMT` | str | `ocr:result:{job_id}` | LIST key (BLPOP) |
| `REDIS_RESULT_TTL_SECONDS` | int | `90` | |
| `REDIS_PHASH_TTL_SECONDS` | int | `86400` | |
| `REDIS_REQUEUE_TTL_SECONDS` | int | `3600` | |
| `POSTGRES_DSN` | str | — | |
| `MINIO_ENDPOINT` | str | — | |
| `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` | str | — | |
| `MINIO_BUCKET_UPLOADS` | str | `invoices` | |
| `MINIO_BUCKET_FAILED` | str | `failed-invoices` | |
| `TRITON_HOST` | str | `triton:8001` | |
| `YOLO_MODEL_NAME` | str | `yolov11n_receipt` | |
| `YOLO_CONFIDENCE_THRESHOLD` | float | `0.35` | |
| `YOLO_CROP_PAD_PERCENT` | float | `0.02` | |
| `MAX_IMAGE_DIMENSION` | int | `1600` | |
| `JPEG_QUALITY` | int | `85` | LLM-only re-encode |
| `GEMINI_API_KEY` | str | — | Native `google-genai` auth |
| `GEMINI_MODEL` | str | `gemini-3.1-flash-lite-preview` | Override to `gemini-2.5-flash-lite` if 3.1-preview is unavailable in your region |
| `GEMINI_TIMEOUT_SECONDS` | int | `15` | Per-call ceiling; Flash Lite p50 ~2 s |
| `GEMINI_BACKOFFS_SECONDS` | list[float] | `[0.3, 0.6, 1.2]` | **JSON list** in env (`GEMINI_BACKOFFS_SECONDS='[0.3,0.6,1.2]'`); pydantic-settings parses natively |
| `PROMPT_SEMANTIC_VERSION` | str | `v3.4` | Drives both cache key **and** prompt file path |
| `WORKER_CONCURRENCY` | int | `4` | Async tasks per process |
| `TOKEN_BUCKET_RPS` | float | `4.0` | Per process |
| `TOKEN_BUCKET_BURST` | int | `8` | Per process |
| `RATE_LIMIT_REFRESH_INTERVAL` | int | `30` | Seconds |
| `WHITELIST_DIR` | str | `/app/whitelists` | |
| `WHITELIST_RELOAD_INTERVAL` | int | `60` | Seconds |
| `SWEEP_INTERVAL_SECONDS` | int | `60` | |
| `STALE_PROCESSING_MINUTES` | int | `15` | |
| `STALE_PENDING_MINUTES` | int | `30` | |
| `REQUEUE_MAX` | int | `3` | |
| `BACKPRESSURE_QUEUE_WARN` | int | `200` | |
| `BACKPRESSURE_QUEUE_REJECT` | int | `500` | HTTP 429 |
| `WORKER_ID` | str | docker hostname | Log correlation + purge selector |
| `PURGE_WORKER_ID` | str | `worker-1` | Nightly 02:00 purge runner |
| `JOB_RETENTION_DAYS` | int | `90` | |

> `WORKER_NUM_PROCESSES` is intentionally **not** a Settings field — replica count is owned by docker-compose. Documented in arch §16 as a docs-only mirror.

### Computed (single source of truth — no duplicate helpers elsewhere)

```python
def redis_result_key(self, job_id: UUID | str) -> str:
    return self.REDIS_RESULT_CHANNEL_FMT.format(job_id=str(job_id))

def phash_cache_key(self, phash: str) -> str:
    return f"ocr:phash:{phash}:psv:{self.PROMPT_SEMANTIC_VERSION}"

def prompt_file_path(self) -> Path:
    return Path(__file__).parent.parent / "pipeline" / "prompts" / f"{self.PROMPT_SEMANTIC_VERSION}.txt"
```

Singleton: `settings = Settings()`.

---

## 3. Schemas (`src/schemas/invoice.py`)

All OCR output fields are **strings** (never None). Missing → `""`.

### `class Product(BaseModel)`
| Field | Type | Contract |
|---|---|---|
| `product_id` | str | NFC-normalized |
| `product_name` | str | Post fuzzy match |
| `product_unit_price` | str | Digits only, optional leading `-` |
| `product_quantity` | str | Numeric; `"10"` not `"10.000"` when integer |
| `product_discount_money` | str | Digits only; `"-24000"` allowed |
| `product_total_money` | str | Digits only |

### `class InvoiceResult(BaseModel)`
| Field | Type | Contract |
|---|---|---|
| `name` | str | Fuzzy-matched |
| `type` | str | Lowercased |
| `date` | str | `DD/MM/YYYY` |
| `time` | str | `HH:MM` or `HH:MM:SS` |
| `pos_id`, `receipt_number`, `cashier`, `barcode` | str | NFC-normalized |
| `total_money` | str | Digits only |
| `products` | list[Product] | Possibly empty |

`model_config = ConfigDict(extra="forbid")`.

### `class JobRecord(BaseModel)`
| Field | Type | Notes |
|---|---|---|
| `job_id` | UUID | |
| `status` | JobStatus | |
| `phash` | str \| None | |
| `minio_key` | str | |
| `failed_minio_key` | str \| None | Set on permanent-failure moves |
| `result` | dict \| None | Final InvoiceResult JSON (status SUCCEEDED only) |
| `error_code` | str \| None | Stable code from exception hierarchy |
| `error_message` | str \| None | Human-readable |
| `created_at` | datetime | |
| `updated_at` | datetime | |

> No `requeue_count` column — bookkeeping is Redis-only (single source of truth, decision §5.3).

### `class ErrorPayload(BaseModel)` — published on terminal failure
```python
job_id: str
status: Literal["FAILED_PERMANENT","FAILED_TRANSIENT"]
error_code: str
error_message: str
```

### `class SuccessPayload(BaseModel)`
```python
job_id: str
status: Literal["SUCCEEDED"] = "SUCCEEDED"
result: dict   # InvoiceResult.model_dump()
```

### `class PendingEnvelope(BaseModel)` — returned on 504 / GET while in-flight
```python
job_id: str
status: Literal["PENDING","PROCESSING"]
message: str
```

---

## 4. Domain (`src/domain`)

### `constants.py`
```python
class JobStatus(str, Enum):
    PENDING           = "PENDING"
    PROCESSING        = "PROCESSING"
    SUCCEEDED         = "SUCCEEDED"
    FAILED_PERMANENT  = "FAILED_PERMANENT"
    FAILED_TRANSIENT  = "FAILED_TRANSIENT"

REDIS_RATE_LIMIT_HASH   = "ocr:rate_limit_config"
REDIS_REQUEUE_HASH_FMT  = "ocr:requeue:{job_id}"
REDIS_REQUEUE_FIELD     = "count"
```

### `errors.py` — see §12.

---

## 5. Storage Clients

### 5.1 `src/storage/minio_client.py` — `class MinIOClient`
Blocking SDK; methods called via `asyncio.to_thread`.

| Method | Signature | Behavior |
|---|---|---|
| `ensure_buckets_exist()` | `() -> None` | **`init` container only.** Idempotent: creates `invoices/` + `failed-invoices/`, sets 30-day lifecycle on `failed-invoices/` |
| `assert_buckets_exist()` | `() -> None` | **api + worker startup.** Read-only check; raises `StorageTransientError` if any bucket missing → fail-fast |
| `upload_file(key, data)` | `(str, bytes) -> None` | `invoices/` bucket |
| `download_file(key)` | `(str) -> bytes` | Raises `ObjectNotFoundError` (permanent) if missing |
| `delete_file(key)` | `(str) -> None` | Best-effort; log on miss |
| `move_to_failed(key)` | `(str) -> str` | Copy to `failed-invoices/`, delete source; return new key. Idempotent: caller guards via `failed_minio_key IS NULL` |
| `head_bucket()` | `() -> bool` | For `/readyz`. Probes BOTH `MINIO_BUCKET_UPLOADS` and `MINIO_BUCKET_FAILED` via the SDK's `bucket_exists()`; returns True iff both exist. Connection error → returns False (do not raise — `/readyz` aggregates) |

Error mapping: network/5xx → `StorageTransientError`; missing object → `ObjectNotFoundError` (permanent).

### 5.2 `src/storage/postgres_client.py` — `class PostgresClient`
`asyncpg` pool. All async. **All `asyncpg.PostgresError`s wrapped to `DatabaseUnavailable`** in a thin decorator (`@_wrap_pg_errors`), so the worker except-tuple is meaningful.

| Method | Signature | Behavior |
|---|---|---|
| `init_pool()` | `() -> None` | |
| `close_pool()` | `() -> None` | |
| `ping()` | `() -> bool` | For `/readyz` |
| `create_job_record(job_id, minio_key, phash)` | `(UUID, str, str\|None) -> None` | INSERT PENDING |
| `update_job_status(job_id, status, **fields)` | `(UUID, JobStatus, ...) -> None` | Partial UPDATE; always stamps `updated_at`. Accepts `result`, `error_code`, `error_message`, `failed_minio_key`, `phash` |
| `touch_updated_at(job_id)` | `(UUID) -> None` | UPDATE … SET updated_at=now(); used by `_yield_to_queue` (decision #35) |
| `get_job_record(job_id)` | `(UUID) -> JobRecord\|None` | |
| `select_stale_jobs()` | `() -> list[JobRecord]` | SELECT PROCESSING WHERE updated_at < now()-15m OR PENDING WHERE created_at < now()-30m. Used by sweeper (which then calls `_fail`-equivalent per row) |
| `purge_old_job_records()` | `() -> int` | Hard delete terminal rows older than `JOB_RETENTION_DAYS` |

> No `mark_stale_jobs_as_failed` bulk UPDATE — sweeper iterates per-row so it can publish per-job (invariant #12).

### 5.3 `src/storage/redis_client.py` — `class RedisClient`
`redis.asyncio` pool. All async.

| Method | Signature | Behavior |
|---|---|---|
| `init()` / `close()` | `() -> None` | Pool lifecycle |
| `ping()` | `() -> bool` | `/readyz` |
| `push_to_queue(job_id)` | `(UUID) -> None` | `LPUSH ocr:queue <job_id>` |
| `pop_from_queue(timeout=5)` | `(int) -> UUID\|None` | `BRPOP ocr:queue` |
| `get_queue_depth()` | `() -> int` | `LLEN` |
| `publish_result(job_id, payload)` | `(UUID, dict) -> None` | `LPUSH settings.redis_result_key(job_id) <json>` then `EXPIRE 90`. API consumes via `BLPOP` |
| `wait_for_result(job_id, timeout)` | `(UUID, int) -> dict\|None` | `BLPOP …`; decode JSON. On `redis.exceptions.ConnectionError` or `TimeoutError` during the BLPOP: log + `ocr_wait_redis_drops_total.inc()` + return `None`. Never re-raises into the API route |
| `get_phash_cache(phash)` | `(str) -> dict\|None` | Key from `settings.phash_cache_key`. Returns raw dict |
| `set_phash_cache(phash, raw_payload)` | `(str, dict) -> None` | `SETEX` TTL `REDIS_PHASH_TTL_SECONDS` |
| `bump_requeue_counter(job_id)` | `(UUID) -> int` | `HINCRBY ocr:requeue:<id> count 1`; on first bump `EXPIRE REDIS_REQUEUE_TTL_SECONDS`. Returns new count |
| `read_rate_limit_config()` | `() -> dict` | `HGETALL ocr:rate_limit_config`; returns `{"rps": float, "burst": int}` or defaults |

> **Result channel is a LIST with BLPOP** (not pub/sub). Survives publish-before-subscribe race. TTL 90 s auto-evicts unclaimed results.

---

## 6. Triton Client (`src/pipeline/triton_client.py`)

### Module-level singleton
```python
_client: tritonclient.grpc.InferenceServerClient | None = None

def get_triton() -> InferenceServerClient:
    global _client
    if _client is None:
        _client = InferenceServerClient(url=settings.TRITON_HOST, verbose=False)
    return _client
```

### `async def infer_yolo(image_chw_fp32: np.ndarray) -> np.ndarray`
- Build `InferInput("images", shape=[1,3,640,640], datatype="FP32")`
- Request output `"output0"`
- `await asyncio.to_thread(client.infer, …)` (sync gRPC call wrapped)
- On `InferenceServerException` connection refused → raise `TritonUnavailableError`
- After response, observe `metrics.triton_batch_size.observe(int(response.get_response().parameters.get("batch_size", 1)))` (best-effort)
- Returns raw output tensor

No YOLO state in worker memory. No `_yolo_inference_lock`. Triton owns concurrency (invariant #13).

> No `readyz()` helper — Triton failures surface as per-job `TritonUnavailableError` rather than blocking `/readyz`.

---

## 7. API Layer

### 7.1 `src/api/app.py`

#### `def create_app() -> FastAPI`
Lifespan:
- **Startup (in order):**
  1. `await pg.init_pool()`
  2. `await redis.init()`
  3. `await asyncio.to_thread(minio.assert_buckets_exist)` — fail-fast
  4. `start_metrics_server(settings.API_METRICS_PORT)`
  5. `sampler_task = asyncio.create_task(queue_depth_sampler())`
- **Shutdown:**
  - cancel `sampler_task`
  - `await pg.close_pool()`
  - `await redis.close()`

Exception handlers (decorated on the app):
```python
@app.exception_handler(UnsupportedMediaType)
async def _h_415(req, exc): return JSONResponse(status_code=415, content={"error_code":exc.error_code,"message":exc.message})

@app.exception_handler(PayloadTooLarge)
async def _h_413(req, exc): return JSONResponse(status_code=413, content={"error_code":exc.error_code,"message":exc.message})

@app.exception_handler(StorageTransientError)
async def _h_503(req, exc): return JSONResponse(status_code=503, content={"error_code":exc.error_code,"message":exc.message})

@app.exception_handler(DatabaseUnavailable)
async def _h_503_db(req, exc): return JSONResponse(status_code=503, content={"error_code":exc.error_code,"message":exc.message})

@app.exception_handler(OCRSystemError)
async def _h_500(req, exc): return JSONResponse(status_code=500, content={"error_code":exc.error_code,"message":exc.message})
```

> **Scoping rule:** `StorageTransientError` / `DatabaseUnavailable` raised during `submit_receipt` (upload/insert/LPUSH) or `get_receipt` (select) or `/readyz` propagate to these API handlers and surface as HTTP 503. Worker-side storage exceptions NEVER reach this handler — they are caught inside `execute_task_lifecycle` and routed through `_fail(FAILED_TRANSIENT)`. The API process and worker process share the exception classes but do not share handlers.

### 7.2 `src/api/routes.py`

#### `async def submit_receipt(file: UploadFile) -> Response`
**Path:** `POST /v1/receipts`

Steps:
1. `await check_backpressure()` — raises `HTTPException(429)` if `LLEN ≥ BACKPRESSURE_QUEUE_REJECT`.
2. Validate `content_type in {"image/jpeg","image/png","image/webp"}` → else `raise UnsupportedMediaType()` (handler → 415).
3. `raw = await file.read()`; if `len(raw) > API_MAX_IMAGE_BYTES` → `raise PayloadTooLarge()` (handler → 413).
4. `job_id = uuid.uuid4()`; derive `ext` from validated `content_type` (`{"image/jpeg":"jpg", "image/png":"png", "image/webp":"webp"}[content_type]`); `minio_key = f"{job_id}/original.{ext}"`.
5. **Ordered start (invariant I1):**
   ```python
   await asyncio.to_thread(minio.upload_file, minio_key, raw)   # must succeed first
   await asyncio.gather(
       pg.create_job_record(job_id, minio_key, phash=None),
       redis.push_to_queue(job_id),
   )
   ```
6. `result = await redis.wait_for_result(job_id, timeout=settings.API_TIMEOUT_SECONDS)` — if the Redis connection drops mid-BLPOP, `redis.exceptions.ConnectionError` is caught inside `wait_for_result` and the method returns `None` (treated as timeout). The job continues worker-side; the client falls through to the 504+poll path. Metric: `ocr_wait_redis_drops_total` incremented on that branch. Under no circumstance does this route raise 5xx to the client while a job is still in flight.
7. **Wire contract (decision #31):**
   - `result is None` → `JSONResponse(status_code=504, content=PendingEnvelope(job_id=str(job_id), status="PENDING", message="poll GET /v1/receipts/{job_id}").model_dump())`. `metrics.requests_total.labels(status="timeout").inc()`.
   - `result["status"] == "SUCCEEDED"` → `JSONResponse(status_code=200, content=result["result"])` — **bare `InvoiceResult`**.
   - `result["status"] == "FAILED_PERMANENT"` → `JSONResponse(status_code=422, content=ErrorPayload(**result).model_dump())`.
   - `result["status"] == "FAILED_TRANSIENT"` → `JSONResponse(status_code=503, content=ErrorPayload(**result).model_dump())`.
8. End-to-end latency observed via `metrics.e2e_latency_seconds` in a `try/finally` wrapper.

#### `async def get_receipt(job_id: UUID) -> Response`
**Path:** `GET /v1/receipts/{job_id}`

- `record = await pg.get_job_record(job_id)`; if None → `HTTPException(404)`.
- HTTP status mirrors `record.status` (decision #27):
  - `SUCCEEDED` → 200, body = bare `record.result` (`InvoiceResult`)
  - `PENDING` or `PROCESSING` → 202, body = `PendingEnvelope(job_id, status, "still processing")`
  - `FAILED_PERMANENT` → 422, body = `ErrorPayload(...)`
  - `FAILED_TRANSIENT` → 503, body = `ErrorPayload(...)`

#### `GET /healthz` — always 200 `{"status":"ok"}`.

#### `GET /readyz`
- `await redis.ping()` AND `await pg.ping()` AND `await asyncio.to_thread(minio.head_bucket)` — all must pass, else 503.

### 7.3 `src/api/backpressure.py`
```python
async def check_backpressure() -> None:
    depth = await redis.get_queue_depth()
    if depth >= settings.BACKPRESSURE_QUEUE_REJECT:
        metrics.backpressure_rejections_total.inc()
        raise HTTPException(status_code=429, detail="system_overloaded",
                            headers={"Retry-After": "5"})
    if depth >= settings.BACKPRESSURE_QUEUE_WARN:
        metrics.queue_soft_warn_total.inc()
```

### 7.4 `src/api/metrics.py`
- `start_metrics_server(port: int)` → `prometheus_client.start_http_server(port)` (its own thread).
- `async def queue_depth_sampler()`: loop `await asyncio.sleep(5); metrics.queue_depth.set(await redis.get_queue_depth())`. Cancels on shutdown.
- All API-side metric objects defined here (counters: `requests_total`, `backpressure_rejections_total`, `queue_soft_warn_total`; gauge `queue_depth`; histogram `e2e_latency_seconds`). See arch §13.1.

### 7.5 `src/worker/metrics.py`
Worker-side metrics (see arch §13.1). Both `api/metrics.py` and `worker/metrics.py` import a shared `prometheus_client` registry but bind to **different ports** (`9101`/`9102`) — no `EADDRINUSE` on shared host networks.

---

## 8. Worker Layer

### 8.1 `src/worker/main.py`

Globals (module-level singletons populated in `run_worker`; events are instantiated inside `run_worker` so they bind to the running loop):
```python
pg: PostgresClient
redis: RedisClient
minio: MinIOClient
token_bucket: TokenBucket
whitelist_index: WhitelistIndex
shutdown_event: asyncio.Event           # asyncio side, instantiated inside run_worker
shutdown_tevent: threading.Event        # mirrored for whitelist thread, also inside run_worker
```

#### `async def run_worker() -> None`
1. `configure_logging(service="worker")`
2. Instantiate event mirrors at the top of the function:
   ```python
   global shutdown_event, shutdown_tevent
   shutdown_event = asyncio.Event()
   shutdown_tevent = threading.Event()
   ```
   (Not module-level, because `asyncio.Event()` requires a running loop.)
3. Initialize all clients (`pg.init_pool`, `redis.init`).
4. `await asyncio.to_thread(minio.assert_buckets_exist)` — fail-fast.
5. `token_bucket = TokenBucket(rps=settings.TOKEN_BUCKET_RPS, burst=settings.TOKEN_BUCKET_BURST)`
6. `whitelist_index = WhitelistIndex.build(settings.WHITELIST_DIR)`
7. Triton warmup: one synthetic zero-image `infer_yolo` call (logged; failure is logged-not-fatal).
8. `start_metrics_server(settings.WORKER_METRICS_PORT)`
9. Spawn daemons:
   - `asyncio.create_task(sweep_stale_jobs_daemon())`
   - `asyncio.create_task(refresh_rate_limit_daemon(token_bucket))`
   - `threading.Thread(target=whitelist_reload_thread, args=(whitelist_index, shutdown_tevent), daemon=True).start()`
   - If `WORKER_ID == PURGE_WORKER_ID`: `asyncio.create_task(nightly_purge_daemon())`
10. Spawn `[asyncio.create_task(worker_loop()) for _ in range(settings.WORKER_CONCURRENCY)]`.
11. `await shutdown_event.wait()`
12. `shutdown_tevent.set()`; cancel asyncio tasks; close pools.

#### Signal handlers
- SIGTERM / SIGINT → `loop.add_signal_handler(sig, lambda: (shutdown_event.set(), shutdown_tevent.set()))` — both events flip together so the whitelist thread wakes promptly.

### 8.2 `src/worker/loop.py`

#### `async def worker_loop() -> None`
```python
while not shutdown_event.is_set():
    job_id = await redis.pop_from_queue(timeout=5)
    if job_id is None:
        continue
    try:
        await execute_task_lifecycle(job_id)
    except Exception:
        logger.exception("unhandled_in_worker_loop", extra={"job_id": str(job_id)})
```

#### `async def execute_task_lifecycle(job_id: UUID) -> None`
See Appendix §19 for the full stub. High-level (note the **single** PROCESSING update — phash is persisted in the SUCCEEDED write, not separately, eliminating a round-trip):

1. `metrics.inflight_jobs.inc()` (try/finally `.dec()` at end)
2. `await pg.update_job_status(job_id, PROCESSING)`
3. `record = await pg.get_job_record(job_id)`; if None → `_orphan(job_id)`; return.
4. `raw_bytes = await asyncio.to_thread(minio.download_file, record.minio_key)` (raises `ObjectNotFoundError` on miss → caught as permanent; `StorageTransientError` on network → caught as transient)
5. `pp = preprocess_image(raw_bytes)` → `(pil, phash)`
6. **pHash cache:**
   ```python
   cached = await redis.get_phash_cache(pp.phash)
   raw: InvoiceResult | None = None
   if cached is not None:
       try:
           raw = InvoiceResult.model_validate(cached)
           metrics.phash_hits.inc()
       except ValidationError:
           metrics.phash_schema_drift.inc()
           raw = None
   ```
7. If `raw is None`:
   - `cropped = await detect_invoice(pp.pil)`
   - `crop_bytes = encode_jpeg(cropped, settings.JPEG_QUALITY)`
   - `metrics.llm_payload_bytes.observe(len(crop_bytes))`
   - `raw = await extract_invoice(crop_bytes, job_id, token_bucket)`
   - `metrics.phash_misses.inc()`
   - `await redis.set_phash_cache(pp.phash, raw.model_dump())`
8. `final = postprocess(raw, whitelist_index)` (always — hot-reload correctness)
9. `success = SuccessPayload(job_id=str(job_id), result=final.model_dump()).model_dump()`
10. `await asyncio.gather(pg.update_job_status(job_id, SUCCEEDED, result=success["result"], phash=pp.phash), redis.publish_result(job_id, success))`
11. `try: await asyncio.to_thread(minio.delete_file, record.minio_key); except: log.warning(...)`

Error catchers (catch, translate, never re-raise out):
- `RateLimitedLocallyError` → `_yield_to_queue(job_id, original_key=record.minio_key)`
- `(PermanentPipelineError, ObjectNotFoundError)` → `_fail(FAILED_PERMANENT, …, move_file=True if PermanentPipelineError else False)`
- `(TransientPipelineError, GeminiExhaustedError, StorageTransientError, TritonUnavailableError, DatabaseUnavailable)` → `_fail(FAILED_TRANSIENT, …, move_file=False)`
- `Exception` → log + `_fail(FAILED_TRANSIENT, "unhandled_exception")`

> Note: `ObjectNotFoundError` is in the **permanent** tuple (decision #34).

#### `async def _fail(job_id, status, *, error_code, error_message, move_file=False, original_key=None) -> None`
See Appendix §19. Idempotent on `move_to_failed`:
```python
if move_file and original_key:
    rec = await pg.get_job_record(job_id)
    if rec and rec.failed_minio_key:
        failed_key = rec.failed_minio_key       # already moved; reuse
    else:
        failed_key = await asyncio.to_thread(minio.move_to_failed, original_key)
```

#### `async def _yield_to_queue(job_id, *, original_key=None) -> None`
```python
count = await redis.bump_requeue_counter(job_id)
metrics.rate_limit_yields_total.inc()
metrics.requeue_count.observe(count)
if count > settings.REQUEUE_MAX:
    await _fail(job_id, JobStatus.FAILED_TRANSIENT,
                error_code="rate_limit_requeue_exhausted",
                error_message="Gemini throttle exceeded max requeues",
                move_file=False, original_key=original_key)
    return
await pg.touch_updated_at(job_id)   # decision #35 — keeps sweeper off
await redis.push_to_queue(job_id)
```

#### `async def _orphan(job_id) -> None`
```python
logger.error("orphan_job", extra={"job_id": str(job_id)})
metrics.orphan_jobs_total.inc()
# No requeue, no PG write (record doesn't exist). Drop.
```

### 8.3 `src/worker/sweeper.py`

#### `async def sweep_stale_jobs_daemon() -> None`
```python
while not shutdown_event.is_set():
    try:
        rows = await pg.select_stale_jobs()
        for r in rows:
            await _fail(
                r.job_id,
                JobStatus.FAILED_TRANSIENT,
                error_code="stale_timeout",
                error_message=f"swept from {r.status.value} after stale window",
                move_file=False,
            )
            metrics.stale_jobs_recovered_total.labels(from_status=r.status.value).inc()
    except Exception:
        logger.exception("sweeper_tick_failed")
    await asyncio.sleep(settings.SWEEP_INTERVAL_SECONDS)
```
**Sweeper publishes via `_fail`** (invariant #12). Publish to a possibly-already-expired result LIST is harmless (Redis silently drops on missing key after EXPIRE).

### 8.4 `src/worker/rate_refresh.py`

#### `async def refresh_rate_limit_daemon(bucket: TokenBucket) -> None`
```python
while not shutdown_event.is_set():
    try:
        cfg = await redis.read_rate_limit_config()
        await bucket.reconfigure(
            rps=float(cfg.get("rps", settings.TOKEN_BUCKET_RPS)),
            burst=int(cfg.get("burst", settings.TOKEN_BUCKET_BURST)),
        )
        metrics.token_bucket_refresh_total.labels(outcome="ok").inc()
        metrics.token_bucket_available.set(bucket.available())
    except Exception:
        metrics.token_bucket_refresh_total.labels(outcome="redis_error").inc()
        logger.exception("rate_refresh_failed")
    await asyncio.sleep(settings.RATE_LIMIT_REFRESH_INTERVAL)
```

### 8.5 `src/worker/whitelist_reload.py`

#### `def whitelist_reload_thread(index: WhitelistIndex, stop: threading.Event) -> None`
Daemon **thread**. Pure mtime check; no asyncio.
```python
while not stop.is_set():
    for kind, filename in [("store","store_names_whitelist.json"),
                           ("product","product_names_whitelist.json")]:
        path = Path(settings.WHITELIST_DIR) / filename
        try:
            mtime = path.stat().st_mtime
            if mtime > index.last_mtime.get(kind, 0):
                index.reload(kind, path)
                metrics.whitelist_reload_total.labels(file=kind).inc()
        except (OSError, json.JSONDecodeError) as e:
            metrics.whitelist_reload_failed_total.labels(
                file=kind, reason=type(e).__name__).inc()
            logger.exception("whitelist_reload_failed", extra={"kind":kind})
    stop.wait(timeout=settings.WHITELIST_RELOAD_INTERVAL)   # interruptible sleep
```
Uses `Event.wait(timeout=...)` instead of `time.sleep`, so SIGTERM exits within seconds.

### 8.6 `src/worker/nightly_purge.py`
```python
async def _sleep_until_next(hhmm: str) -> None:
    """Sleep until the next occurrence of HH:MM in local time."""
    h, m = (int(x) for x in hhmm.split(":"))
    now = datetime.now()
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    await asyncio.sleep((target - now).total_seconds())

async def nightly_purge_daemon():
    while not shutdown_event.is_set():
        await _sleep_until_next("02:00")
        try:
            deleted = await pg.purge_old_job_records()
            logger.info("purge_done", extra={"rows": deleted})
        except Exception:
            logger.exception("purge_failed")
```

---

## 9. Pipeline Layer

### 9.1 `src/pipeline/preprocessor.py`

#### `@dataclass class PreprocessResult`
```python
pil: Image.Image
phash: str
```
(Minimal — `original_size` and `oriented` removed; never consumed downstream. Available via structured logs if needed.)

#### `def preprocess_image(raw: bytes) -> PreprocessResult`
1. `img = Image.open(io.BytesIO(raw))`
2. `img.load()` (catches truncated → raises `PermanentPipelineError("truncated_upload")`)
3. `oriented = ImageOps.exif_transpose(img)`
4. `rgb = oriented.convert("RGB")`
5. If `max(rgb.size) > MAX_IMAGE_DIMENSION`: `rgb.thumbnail((MAX,MAX), Image.LANCZOS)`
6. `phash = str(imagehash.phash(rgb))`
7. Return `PreprocessResult(rgb, phash)`

### 9.2 `src/pipeline/detector.py`

#### `async def detect_invoice(image: Image.Image) -> Image.Image`
Returns the **cropped PIL image directly** — bbox/confidence are observed via metrics + logs but not surfaced (simpler call site).

1. `x = preprocess_for_triton(image)` → numpy FP32 `[1,3,640,640]`; remember `(orig_w, orig_h)`
2. `output = await infer_yolo(x)` (see §6)
3. Decode boxes from `[-1, 6]` (`x1,y1,x2,y2,conf,cls`)
4. Filter `conf >= settings.YOLO_CONFIDENCE_THRESHOLD`
5. If empty → `metrics.yolo_rejection_total.inc()`; raise `PermanentPipelineError("yolo_no_detection")`
6. **argmax** on conf (no NMS)
7. Scale bbox → original; pad `YOLO_CROP_PAD_PERCENT`; clamp
8. `cropped = image.crop(bbox)`
9. Log `event=detect, conf=…, bbox=…`; return `cropped`

#### `def encode_jpeg(img: Image.Image, quality: int) -> bytes`
```python
buf = io.BytesIO()
img.save(buf, format="JPEG", quality=quality, optimize=True)
return buf.getvalue()
```

### 9.3 `src/pipeline/extractor.py`

#### Module constants
- `LEGACY_JSON_SCHEMA` — dict imported from `src/pipeline/json_schema.py`. Strict, `additionalProperties:false`. Passed to `response_schema` on every call.
- `SYSTEM_PROMPT` is loaded **lazily** inside `_load_system_prompt()` on first call — NOT at import time. This keeps `src/pipeline/extractor.py` importable from the API container, which does not mount `prompts/`. Loaded value is cached in a module-level `_SYSTEM_PROMPT: str | None`.

```python
_SYSTEM_PROMPT: str | None = None

def _load_system_prompt() -> str:
    global _SYSTEM_PROMPT
    if _SYSTEM_PROMPT is None:
        path = settings.prompt_file_path()
        if not path.exists():
            raise PermanentPipelineError(
                "prompt_file_missing",
                f"Expected prompt file at {path} — PSV={settings.PROMPT_SEMANTIC_VERSION}")
        _SYSTEM_PROMPT = path.read_text(encoding="utf-8")
    return _SYSTEM_PROMPT
```

> Import-safety rule: no code path in `src/pipeline/extractor.py` may read the filesystem or open network sockets at import time. Both the prompt file and the `genai.Client` are lazy-initialized on first `extract_invoice` call. API container startup does not need `prompts/` mounted or `GEMINI_API_KEY` set.

#### Singleton
```python
from google import genai
from google.genai import types, errors as genai_errors

_client: genai.Client | None = None

def get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=settings.GEMINI_API_KEY)
    return _client
```

#### `async def extract_invoice(jpeg_bytes: bytes, job_id: UUID, bucket: TokenBucket) -> InvoiceResult`
1. `if not await bucket.try_acquire(): metrics.token_bucket_acquire_total.labels(outcome="empty").inc(); raise RateLimitedLocallyError("token_bucket_empty")`
2. `metrics.token_bucket_acquire_total.labels(outcome="ok").inc()`
3. `system_prompt = _load_system_prompt()` — lazy-loaded on first call; raises `PermanentPipelineError("prompt_file_missing")` if the file is absent (surfaces as FAILED_PERMANENT, never silent)
4. Build contents:
   ```python
   contents = [
       system_prompt + "\n\nExtract the invoice as JSON.",
       types.Part.from_bytes(data=jpeg_bytes, mime_type="image/jpeg"),
   ]
   config = types.GenerateContentConfig(
       response_mime_type="application/json",
       response_schema=LEGACY_JSON_SCHEMA,
       temperature=0,
   )
   ```
5. Retry loop:
   ```python
   client = get_client()
   for attempt, delay in enumerate(settings.GEMINI_BACKOFFS_SECONDS):
       try:
           resp = await asyncio.wait_for(
               client.aio.models.generate_content(
                   model=settings.GEMINI_MODEL,
                   contents=contents,
                   config=config,
               ),
               timeout=settings.GEMINI_TIMEOUT_SECONDS,
           )
           break
       except genai_errors.ClientError as e:
           if getattr(e, "code", None) == 429:
               raise RateLimitedLocallyError("gemini_rate_limited")
           # 4xx other than 429 → permanent
           raise PermanentPipelineError("gemini_client_error", str(e))
       except (genai_errors.ServerError, asyncio.TimeoutError) as e:
           metrics.gemini_retries_total.labels(attempt=str(attempt)).inc()
           if attempt == len(settings.GEMINI_BACKOFFS_SECONDS) - 1:
               raise GeminiExhaustedError(str(e))
           await asyncio.sleep(delay)
   ```
6. Parse `resp.text` as JSON.
7. `result = InvoiceResult.model_validate(parsed)` — on `ValidationError`: raise `PermanentPipelineError("extractor_invalid_json")`
8. `metrics.extraction_store_type_total.labels(type=result.type or "unknown").inc()`
9. If `resp.usage_metadata` present: observe token counts on `ocr_gemini_tokens_total{kind=prompt|output}` for cost telemetry
10. Strangler Stage A only: for each new optional field, `if getattr(result, f) != "": metrics.new_field_present_total.labels(field=f).inc()`
11. Return `result`

### 9.4 `src/pipeline/postprocessor.py`

Public:
```python
def postprocess(result: InvoiceResult, index: WhitelistIndex) -> InvoiceResult
```
Whitelist passed in (no module-global; avoids API-side build).

Steps (order per arch §5.5):
1. `type` → strip + lower
2. `name` → `index.match_store(...)` (NFC pre-normalization inside)
3. Date/time/pos_id/receipt_number/cashier/barcode → respective normalizers
4. `total_money` → `_normalize_money`
5. For each product: `_normalize_product(p, index)`

Wrap with `metrics.postprocess_duration_seconds` histogram timer.

Private helpers (signatures stable):
- `_normalize_unicode(text: str) -> str`
- `_normalize_money(value: str) -> str`
- `_normalize_date(date_str: str) -> str`
- `_normalize_time(time_str: str) -> str`
- `_normalize_quantity(qty_str: str) -> str`
- `_normalize_product(p: Product, index: WhitelistIndex) -> Product`

### 9.5 `src/pipeline/whitelist_index.py` — `class WhitelistIndex`

Two sub-indexes (`store`, `product`), unified internal layout per kind:
```python
_buckets: dict[tuple[str,int], list[tuple[str,str]]]   # (first3, len//4) → [(lower, canonical)]
_all_lower: list[str]                                  # fallback scan
_canonical_of: dict[str, str]
last_mtime: dict[str, float]
source_path: dict[str, Path]
_lock: threading.Lock
```

Methods (no orphans):
| Method | Signature | Behavior |
|---|---|---|
| `build(whitelist_dir)` (classmethod) | `(str) -> WhitelistIndex` | Load both files; build buckets; remember paths + mtimes |
| `reload(kind, path)` | `(str, Path) -> None` | Atomic swap of `(_buckets, _all_lower, _canonical_of)` triple under `_lock`; updates `last_mtime` |
| `match_store(raw)` | `(str) -> str` | Cutoff 80; fallback full-scan at 60 |
| `match_product(raw)` | `(str) -> str` | Cutoff 70; **no fallback** (returns NFC raw on miss) |

> `build` calls a private `_load_one(kind, path)` per kind.

Matching algorithm:
```
if not raw: return raw
normalized = unicodedata.normalize("NFC", raw).strip()
lower = normalized.lower()
if not lower: return normalized
k0, k1 = lower[:3], len(lower) // 4
candidates = []
for delta in (-1, 0, 1):
    candidates.extend(self._buckets.get((k0, k1 + delta), []))

primary, fallback = (80, 60) if kind == "store" else (70, None)

if not candidates:
    if fallback is None:
        metrics.whitelist_match_total.labels(field=kind, tier="miss").inc()
        return normalized
    best, score, _ = (rapidfuzz.process.extractOne(lower, self._all_lower,
                          scorer=rapidfuzz.fuzz.WRatio) or (None, 0, None))
    if best and score >= fallback:
        metrics.whitelist_match_total.labels(field=kind, tier="fuzzy_low").inc()
        return self._canonical_of[best]
    metrics.whitelist_match_total.labels(field=kind, tier="miss").inc()
    return normalized

best, score, _ = rapidfuzz.process.extractOne(
    lower, [c[0] for c in candidates], scorer=rapidfuzz.fuzz.WRatio)
if score >= primary:
    tier = "exact" if score == 100 else "fuzzy_high"
    metrics.whitelist_match_total.labels(field=kind, tier=tier).inc()
    return self._canonical_of[best]
metrics.whitelist_match_total.labels(field=kind, tier="miss").inc()
return normalized
```
(Product fallback: `fallback is None` → return NFC raw immediately if no candidates.)

---

## 10. Utility Classes

### 10.1 `src/utils/token_bucket.py` — `class TokenBucket`
```python
class TokenBucket:
    def __init__(self, rps: float, burst: int):
        self._rps = rps
        self._burst = burst
        self._tokens = float(burst)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def try_acquire(self, n: int = 1) -> bool:
        async with self._lock:
            now = time.monotonic()
            self._tokens = min(self._burst, self._tokens + (now - self._last) * self._rps)
            self._last = now
            if self._tokens >= n:
                self._tokens -= n
                return True
            return False

    async def reconfigure(self, rps: float, burst: int) -> None:
        async with self._lock:                # held during the swap (correct in single-loop world)
            self._rps = rps
            self._burst = burst
            if self._tokens > burst:
                self._tokens = float(burst)

    def available(self) -> float:
        # Best-effort snapshot for the gauge. No lock — float reads are atomic in CPython.
        return self._tokens
```
(`reconfigure` takes the lock; `available()` is documented as best-effort gauge sampler.)

> No `src/utils/phash_cache.py`. Use `settings.phash_cache_key()` (single source of truth).

---

## 11. Background Tasks & Daemons

| Daemon | Where | Cadence | Purpose |
|---|---|---|---|
| Stale sweeper | worker, all processes | 60 s | Per-row `_fail(FAILED_TRANSIENT, "stale_timeout")` for stale rows |
| Rate-limit refresh | worker, all processes | 30 s | Sync TokenBucket from Redis hash |
| Whitelist hot-reload | worker, all processes | 60 s | mtime poll; atomic swap |
| Queue-depth sampler | api, 1 per process | 5 s | Gauge `ocr_queue_depth` |
| Nightly purge | worker, only on `PURGE_WORKER_ID` | daily 02:00 local | Delete terminal rows > `JOB_RETENTION_DAYS` |

Asyncio daemons exit when `shutdown_event` is set. The whitelist thread exits on `shutdown_tevent.set()` mirrored from the asyncio event in `run_worker()`.

---

## 12. Exception Hierarchy (`src/domain/errors.py`)

```
OCRSystemError (base; {error_code, message, is_permanent})
├── IngressError
│   ├── UnsupportedMediaType   (permanent, HTTP 415, error_code="unsupported_media_type")
│   └── PayloadTooLarge        (permanent, HTTP 413, error_code="payload_too_large")
├── PipelineError
│   ├── PermanentPipelineError   # truncated_upload, yolo_no_detection, extractor_invalid_json
│   ├── TransientPipelineError   # base for retryables (used as catch-tuple member)
│   ├── RateLimitedLocallyError  # token bucket OR Gemini RateLimitError
│   ├── GeminiExhaustedError     # 3 attempts exhausted (subclass of TransientPipelineError)
│   └── TritonUnavailableError   # gRPC unreachable (subclass of TransientPipelineError)
└── StorageError
    ├── ObjectNotFoundError      # permanent — file gone from MinIO
    ├── StorageTransientError    # network/5xx (any storage backend)
    └── DatabaseUnavailable      # asyncpg PostgresError (raised via _wrap_pg_errors)
```

Every class exposes `error_code: str`, `message: str`, `is_permanent: bool` (class attribute).

**Producer sites:**
| Class | Raised by |
|---|---|
| `UnsupportedMediaType` | `submit_receipt` step 2 |
| `PayloadTooLarge` | `submit_receipt` step 3 |
| `PermanentPipelineError` | `preprocess_image` (truncated), `detect_invoice` (no detection), `extract_invoice` (invalid JSON) |
| `TransientPipelineError` | Catch-tuple parent only (no direct raise — intentional taxonomy node, documented) |
| `RateLimitedLocallyError` | `extract_invoice` (token bucket empty OR `google.genai.errors.ClientError` 429) |
| `GeminiExhaustedError` | `extract_invoice` retry loop final attempt |
| `TritonUnavailableError` | `infer_yolo` on `InferenceServerException` connection refused |
| `ObjectNotFoundError` | `MinIOClient.download_file` |
| `StorageTransientError` | `MinIOClient.assert_buckets_exist`, `upload_file`, `download_file`, `move_to_failed` on network/5xx; mapped from `redis.exceptions.ConnectionError` in `RedisClient` |
| `DatabaseUnavailable` | `PostgresClient` methods via `@_wrap_pg_errors` decorator |

**Consumer sites (every class caught somewhere):**
- `UnsupportedMediaType`, `PayloadTooLarge`, `StorageTransientError`, `OCRSystemError` → API exception handlers (§7.1).
- `RateLimitedLocallyError` → `execute_task_lifecycle` → `_yield_to_queue`.
- `PermanentPipelineError`, `ObjectNotFoundError` → `execute_task_lifecycle` → `_fail(FAILED_PERMANENT)`.
- `TransientPipelineError`, `GeminiExhaustedError`, `StorageTransientError`, `TritonUnavailableError`, `DatabaseUnavailable` → `execute_task_lifecycle` → `_fail(FAILED_TRANSIENT)`.

---

## 13. Function Dependency Graph

```
API.submit_receipt (POST /v1/receipts)
 ├── check_backpressure ──► redis.get_queue_depth
 ├── minio.upload_file
 ├── asyncio.gather(pg.create_job_record, redis.push_to_queue)
 └── redis.wait_for_result (BLPOP, 60 s)
     ├── None       → 504 + PendingEnvelope
     ├── SUCCEEDED  → 200 + bare InvoiceResult
     ├── FAILED_PERMANENT → 422 + ErrorPayload
     └── FAILED_TRANSIENT → 503 + ErrorPayload

API.get_receipt (GET /v1/receipts/{id})
 └── pg.get_job_record
     ├── None       → 404
     ├── SUCCEEDED  → 200 + bare InvoiceResult
     ├── PENDING|PROCESSING → 202 + PendingEnvelope
     ├── FAILED_PERMANENT → 422 + ErrorPayload
     └── FAILED_TRANSIENT → 503 + ErrorPayload

worker_loop
 └── execute_task_lifecycle [inflight_jobs.inc/.dec]
      ├── pg.update_job_status(PROCESSING)
      ├── pg.get_job_record        ──► _orphan if None
      ├── minio.download_file       (ObjectNotFoundError → FAILED_PERMANENT)
      ├── preprocess_image          (truncated → FAILED_PERMANENT)
      ├── redis.get_phash_cache    (drift → re-extract)
      ├── detect_invoice           ──► triton_client.infer_yolo
      ├── encode_jpeg
      ├── extract_invoice          ──► TokenBucket.try_acquire, genai.Client.aio
      ├── redis.set_phash_cache
      ├── postprocess              ──► WhitelistIndex.match_{store,product}
      ├── asyncio.gather(pg.update_job_status(SUCCEEDED, phash=…), redis.publish_result)
      └── minio.delete_file        (after publish; failure logged-not-fatal)

Error catchers (inside execute_task_lifecycle):
  RateLimitedLocallyError                                        → _yield_to_queue
  PermanentPipelineError | ObjectNotFoundError                   → _fail(FAILED_PERMANENT, move_file=PermanentPipelineError)
  TransientPipelineError | GeminiExhaustedError
    | StorageTransientError | TritonUnavailableError
    | DatabaseUnavailable                                        → _fail(FAILED_TRANSIENT)
  Exception                                                       → _fail(FAILED_TRANSIENT, "unhandled_exception")

Daemons:
  sweeper         → pg.select_stale_jobs → _fail per row (FAILED_TRANSIENT, "stale_timeout")
  rate_refresh    → redis.read_rate_limit_config → TokenBucket.reconfigure → token_bucket_available.set
  whitelist_reload→ WhitelistIndex.reload
  nightly_purge   → pg.purge_old_job_records
```

---

## 14. Invariants (never violate)

1. **Publish-before-delete on SUCCESS.** `asyncio.gather(pg_write, publish)` must resolve before `delete_file`.
2. **Upload-before-enqueue on INGRESS.** MinIO upload is awaited first; PG insert and LPUSH run in `gather` after.
3. **No `time.sleep` in asyncio code.** Only `asyncio.sleep`. (Whitelist thread uses `Event.wait(timeout=…)`.)
4. **All-string OCR outputs.** Missing → `""`, never `null`.
5. **pHash cache stores RAW.** Postprocess always runs. Cache-hit drift → re-extract + overwrite.
6. **Cache key carries `PROMPT_SEMANTIC_VERSION`.** Cosmetic edits do not bump.
7. **PSV ⇔ prompt file path.** Bumping PSV is a code-coupled deploy (`prompts/{PSV}.txt` must exist).
8. **Requeue bounded.** `REQUEUE_MAX=3`; exceed → `FAILED_TRANSIENT` via `_fail`.
9. **`additionalProperties:false`** in both Pydantic `InvoiceResult` and Gemini json_schema (outside strangler Stage A).
10. **Strict YOLO.** No detection → `FAILED_PERMANENT`; no fallback to full image.
11. **WhitelistIndex is atomic-per-kind.** Swap the `(_buckets, _all_lower, _canonical_of)` triple under `threading.Lock`.
12. **Orphan jobs are not requeued.** Log + metric + drop.
13. **Publish-on-failure too.** Every terminal-failure path — including the sweeper — calls `_fail`, which publishes an `ErrorPayload`.
14. **Triton is the single YOLO concurrency point.** Workers hold no model state, no inference lock.
15. **`ensure_buckets_exist` runs only in the `init` container.** `api` and `worker` use `assert_buckets_exist` (read-only).
16. **API and worker bind different metrics ports.** `9101` vs `9102`.
17. **HTTP status mirrors job status on `GET /v1/receipts/{id}`.** Clients branch on the code, not the body.
18. **`_yield_to_queue` touches `updated_at`.** Live-but-throttled jobs are not swept as dead.
19. **`move_to_failed` is idempotent at the call site.** Guarded by `failed_minio_key IS NULL`.

---

## 15. Test Plan Hooks

**Unit**
- `test_postprocessor.py` — all normalizers + Vietnamese edge cases (`"1.356.000"`, `"10.000"` qty, `"-24,000"`, `DD.MM.YYYY`, etc.)
- `test_whitelist_index.py` — bucket hit, fallback to full scan, reload swaps canonical, cutoff boundaries, bucket-drift (±1), product-no-fallback path
- `test_token_bucket.py` — burst exhaustion, refill, `reconfigure` shrinks tokens under lock, concurrent `try_acquire`
- `test_preprocessor.py` — EXIF 3/6/8; phash stability vs resize; max-dim cap; truncated bytes → `PermanentPipelineError`
- `test_schemas.py` — `extra="forbid"`; missing → `""`; `PendingEnvelope`, `ErrorPayload`, `SuccessPayload` round-trip
- `test_errors.py` — every exception sets `error_code`, `is_permanent`; producer site tested (mock raise)
- `test_settings.py` — `prompt_file_path()` resolves; `phash_cache_key()` includes PSV; `GEMINI_BACKOFFS_SECONDS` parses from JSON env

**Integration** (docker-compose test target)
- `test_worker_lifecycle.py` — happy, pHash hit, **schema-drift re-extract**, rate-limit yield, max-requeue → FAILED_TRANSIENT, no-YOLO → FAILED_PERMANENT + move_to_failed + publish, **orphan-job**, **object-not-found → FAILED_PERMANENT**, publish-before-delete invariant
- `test_api_ingress.py` — 200 happy (bare InvoiceResult), 413 oversize, 415 wrong type, 422 on FAILED_PERMANENT, 503 on FAILED_TRANSIENT, 504 + `GET /v1/receipts/{id}` poll → 202 then 200/422/503
- `test_api_backpressure.py` — 429 at `LLEN≥500`, 200 at 499, soft-warn metric at 200
- `test_stale_sweeper.py` — stuck PROCESSING reclaimed after 15 min and **publishes ErrorPayload**; PENDING after 30 min same
- `test_yield_touches_updated_at.py` — yielded job stays out of sweeper window
- `test_triton_batching.py` — 8 concurrent requests form batches ≥ 4 (histogram p50 > 1)
- `test_readyz.py` — 503 when any backend down
- `test_metrics_ports.py` — both `:9101` and `:9102` reachable; no collision in compose

**Contract**
- `test_llm_schema_strangler.py` — Stage A (`strict=false`) accepts optional new field with `new_field_present_total` increment; Stage B (`strict=true`) requires it; PSV bump invalidates cache and requires matching prompt file

---

## 16. Development Order (Milestones)

| # | Milestone | Deliverables | Gate |
|---|---|---|---|
| M0 | Skeleton | Repo layout, `settings.py`, `schemas`, `errors`, `constants`, `docker-compose.yml` (pg+redis+minio+triton+init) | `pytest -q` collects green; `init` creates buckets |
| M1 | Storage | MinIO/Postgres/Redis clients + `assert_buckets_exist` + `_wrap_pg_errors` + schema migration | Round-trip upload/download, INSERT/SELECT, LPUSH/BRPOP pass; missing bucket → fail-fast |
| M2 | Pipeline core | `preprocess_image`, `postprocess(result, index)`, `WhitelistIndex`, all `_normalize_*` | Unit tests pass; whitelist match metrics emitted |
| M3 | Triton integration | `triton_client`, `detect_invoice`, ONNX YOLO mounted; warmup | `ocr_triton_batch_size` shows ≥4 at 8-concurrent load |
| M4 | Extractor | `genai.Client` singleton, `TokenBucket`, retry/yield logic, `prompts/{PSV}.txt` loader | Mock-server: `ClientError(429)` triggers `RateLimitedLocallyError`; 3-retry path works on `ServerError`/timeout; missing prompt file raises `PermanentPipelineError` on first call (not at import) |
| M5 | Worker loop + RPC | `worker_loop`, `execute_task_lifecycle`, `_fail`, `_yield_to_queue`, `_orphan`, cache w/ schema-drift handler, `touch_updated_at` on yield | Submit via API → result < 60 s; failure paths publish ErrorPayload; ObjectNotFound → FAILED_PERMANENT |
| M6 | Daemons | Sweeper (publishes!), rate-refresh, whitelist reload thread, queue-depth sampler, nightly purge | Chaos: kill worker mid-job → reclaimed after 15 min **with publish** |
| M7 | Backpressure + Polling | HTTP 429; `GET /v1/receipts/{id}` with status-mirroring codes; 504 + poll contract | Load test: `LLEN=500` → 429; 504 path → 202 then terminal |
| M8 | Exception handlers + Metrics ports | 413/415/503/500 handlers wired; `api:9101` / `worker:9102` separation | curl wrong content-type → 415; both Prom endpoints scrape |
| M9 | Metrics + Grafana | All metrics from arch §13.1 wired with documented emission sites; 14 Grafana panels provisioned | Dashboard loads; alerts fire in chaos tests; no orphan metric |
| M10 | Strangler + Hardening | PSV cache partitioning + matching prompt file; Stage-A/B field rollout runbook; graceful shutdown drain; structured logging with `job_id`; runbooks (stuck jobs / Gemini outage / Triton outage / Redis split-brain) | Production-readiness checklist signed off |

---

## 17. Init Container & DB Migrations

### 17.1 `migrations/` (alembic) — sole source of schema truth
Layout:
```
migrations/
├── alembic.ini
├── env.py              # reads settings.POSTGRES_DSN
├── script.py.mako
└── versions/
    └── 0001_initial_jobs.py      # creates jobs table + partial index
```

`0001_initial_jobs.py` creates:
- `jobs` table with columns per §3 `JobRecord` (`job_id UUID PK`, `status TEXT NOT NULL`, `phash TEXT NULL`, `minio_key TEXT NOT NULL`, `failed_minio_key TEXT NULL`, `result JSONB NULL`, `error_code TEXT NULL`, `error_message TEXT NULL`, `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`, `updated_at TIMESTAMPTZ NOT NULL DEFAULT now()`)
- Partial index: `CREATE INDEX jobs_stale_idx ON jobs (status, updated_at) WHERE status IN ('PROCESSING','PENDING')` — drives sweeper query (arch §4.4)
- `CHECK (status IN ('PENDING','PROCESSING','SUCCEEDED','FAILED_PERMANENT','FAILED_TRANSIENT'))`
- `CHECK (status <> 'SUCCEEDED' OR result IS NOT NULL)` — enforces invariant that SUCCEEDED rows carry a result

### 17.2 `src/init/entrypoint.py` — init container entrypoint
Sole caller of MinIO bucket creation and alembic upgrade. Exits non-zero on any failure so docker-compose's `depends_on: condition: service_completed_successfully` gates `api` and `worker` correctly.

```python
# src/init/entrypoint.py
async def main() -> int:
    try:
        # 1. DB migrations (alembic upgrade head) — synchronous
        from alembic.config import Config
        from alembic import command
        cfg = Config("migrations/alembic.ini")
        cfg.set_main_option("sqlalchemy.url", settings.POSTGRES_DSN)
        command.upgrade(cfg, "head")

        # 2. MinIO buckets + lifecycle (sole caller of ensure_buckets_exist)
        await asyncio.to_thread(minio.ensure_buckets_exist)
        await asyncio.to_thread(minio.configure_lifecycles)   # 30d on failed-invoices/; 7d floor on invoices/

        # 3. Readiness probes to catch config errors before releasing
        assert await redis.ping()
        assert await pg.ping()
        return 0
    except Exception:
        logger.exception("init_container_failed")
        return 1

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
```

Docker image `CMD: ["python", "-m", "src.init.entrypoint"]`.

### 17.3 `MinIOClient.configure_lifecycles()` (new, init-only)
Sets two lifecycle rules idempotently:
- `failed-invoices/` — expiration after 30 days (PII-safe window, arch §4.4)
- `invoices/` — expiration after 7 days (floor guaranteeing orphaned blobs from a PG outage during `_fail` cannot leak forever; 7 d is longer than any realistic operator intervention window and much longer than the SLA)

Worker and API do NOT call this method.

---

## 18. Logging Configuration

Structured JSON logging configured at process start via `src/logging_config.py::configure_logging(service: str)`. Called as the first action in:
- `create_app()` lifespan (service="api")
- `run_worker()` (service="worker")
- `src/init/entrypoint.py::main()` (service="init")

Format (stdlib `logging` with a JSON formatter; python-json-logger):
```json
{"ts":"2026-04-19T12:00:00.123Z","level":"INFO","service":"worker",
 "worker_id":"worker-3","job_id":"…","event":"…","duration_ms":820}
```

Rules:
- All log records pass through a `ContextFilter` that injects `service`, `worker_id`, and the `job_id` contextvar when present.
- `LOG_LEVEL` env var (default `INFO`). `DEBUG` enables per-stage entry/exit logs.
- No bare `print`. No f-string interpolation of secrets (GEMINI_API_KEY, POSTGRES_DSN password).
- stdout only; the container runtime captures it. No log files, no rotation.

---

## 19. Appendix — Example Stubs

### `execute_task_lifecycle`
```python
async def execute_task_lifecycle(job_id: UUID) -> None:
    metrics.inflight_jobs.inc()
    record = None
    try:
        await pg.update_job_status(job_id, JobStatus.PROCESSING)
        record = await pg.get_job_record(job_id)
        if record is None:
            await _orphan(job_id)
            return

        try:
            with metrics.stage_duration_seconds.labels(stage="download").time():
                raw_bytes = await asyncio.to_thread(minio.download_file, record.minio_key)

            with metrics.stage_duration_seconds.labels(stage="preprocess").time():
                pp = preprocess_image(raw_bytes)

            with metrics.stage_duration_seconds.labels(stage="phash_lookup").time():
                cached = await redis.get_phash_cache(pp.phash)

            raw: InvoiceResult | None = None
            if cached is not None:
                try:
                    raw = InvoiceResult.model_validate(cached)
                    metrics.phash_hits.inc()
                except ValidationError:
                    metrics.phash_schema_drift.inc()
                    raw = None

            if raw is None:
                metrics.phash_misses.inc()
                with metrics.stage_duration_seconds.labels(stage="yolo").time():
                    cropped = await detect_invoice(pp.pil)
                crop_bytes = encode_jpeg(cropped, settings.JPEG_QUALITY)
                metrics.llm_payload_bytes.observe(len(crop_bytes))
                with metrics.stage_duration_seconds.labels(stage="gemini").time():
                    raw = await extract_invoice(crop_bytes, job_id, token_bucket)
                await redis.set_phash_cache(pp.phash, raw.model_dump())

            with metrics.stage_duration_seconds.labels(stage="postprocess").time():
                final = postprocess(raw, whitelist_index)

            success = SuccessPayload(job_id=str(job_id),
                                     result=final.model_dump()).model_dump()

            with metrics.stage_duration_seconds.labels(stage="publish").time():
                await asyncio.gather(
                    pg.update_job_status(job_id, JobStatus.SUCCEEDED,
                                         result=success["result"], phash=pp.phash),
                    redis.publish_result(job_id, success),
                )
            try:
                await asyncio.to_thread(minio.delete_file, record.minio_key)
            except Exception:
                logger.warning("delete_after_success_failed",
                               extra={"job_id": str(job_id), "key": record.minio_key})

        except RateLimitedLocallyError:
            await _yield_to_queue(job_id, original_key=record.minio_key)

        except (PermanentPipelineError, ObjectNotFoundError) as e:
            await _fail(job_id, JobStatus.FAILED_PERMANENT,
                        error_code=e.error_code, error_message=str(e),
                        move_file=isinstance(e, PermanentPipelineError),
                        original_key=record.minio_key)

        except (TransientPipelineError, GeminiExhaustedError,
                StorageTransientError, TritonUnavailableError,
                DatabaseUnavailable) as e:
            await _fail(job_id, JobStatus.FAILED_TRANSIENT,
                        error_code=e.error_code, error_message=str(e),
                        move_file=False, original_key=record.minio_key)

        except Exception as e:
            logger.exception("unhandled_worker_error",
                             extra={"job_id": str(job_id)})
            await _fail(job_id, JobStatus.FAILED_TRANSIENT,
                        error_code="unhandled_exception",
                        error_message=repr(e),
                        move_file=False, original_key=(record.minio_key if record else None))
    finally:
        metrics.inflight_jobs.dec()
```

### `_fail`
```python
async def _fail(job_id: UUID,
                status: JobStatus,
                *,
                error_code: str,
                error_message: str,
                move_file: bool = False,
                original_key: str | None = None) -> None:
    """
    Publish-safe terminal failure handler. NEVER re-raises — even if PG or Redis
    are down. Each side-effect is independently guarded so a partial outage cannot
    cause recursion back into the worker_loop exception handler.
    """
    failed_key: str | None = None

    # --- idempotent move (guarded by failed_minio_key IS NULL) ---
    if move_file and original_key:
        try:
            existing = await pg.get_job_record(job_id)
            if existing and existing.failed_minio_key:
                failed_key = existing.failed_minio_key
            else:
                failed_key = await asyncio.to_thread(minio.move_to_failed, original_key)
        except Exception:
            logger.exception("move_to_failed_failed",
                             extra={"job_id": str(job_id), "key": original_key})
            # fall through — we still must publish + update PG

    payload = ErrorPayload(job_id=str(job_id),
                           status=status.value,
                           error_code=error_code,
                           error_message=error_message).model_dump()

    # --- independent best-effort side effects; never re-raise ---
    pg_task = pg.update_job_status(job_id, status,
                                   error_code=error_code,
                                   error_message=error_message,
                                   failed_minio_key=failed_key)
    pub_task = redis.publish_result(job_id, payload)
    results = await asyncio.gather(pg_task, pub_task, return_exceptions=True)
    for name, res in zip(("pg_update_failed", "publish_failed"), results):
        if isinstance(res, Exception):
            logger.exception(name, extra={"job_id": str(job_id),
                                          "error_code": error_code})
            metrics.fail_side_effect_errors_total.labels(side=name).inc()
    # Control returns normally regardless of subsystem health.
```

> Contract: `_fail` MUST NOT raise. `worker_loop`'s catch-all relies on this; if `_fail`
> itself raised, a subsequent call from the outer handler could recurse. Any internal
> exception is logged + metered and swallowed.

---

*End of task_v3_final.md. Paired with `architecture_v3_final.md`.*
