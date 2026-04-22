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
