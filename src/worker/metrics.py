"""Worker-side Prometheus metrics.

All metrics are defined here and imported by their emission sites.
Counter/Histogram/Gauge objects are created lazily so importing this module
doesn't register duplicates under pytest reruns.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, start_http_server

# ---- stage timings ----
stage_duration_seconds = Histogram(
    "ocr_stage_duration_seconds",
    "Per-stage wall-clock latency in execute_task_lifecycle",
    ["stage"],
)

# ---- CDN download ----
cdn_download_seconds = Histogram("ocr_cdn_download_seconds", "CDN image download latency")

# ---- pHash cache ----
phash_hits = Counter("ocr_phash_hits_total", "pHash cache hits")
phash_misses = Counter("ocr_phash_misses_total", "pHash cache misses")
phash_schema_drift = Counter("ocr_phash_schema_drift_total", "Cached JSON failed InvoiceResult.model_validate")

# ---- YOLO / Triton ----
yolo_rejection_total = Counter("ocr_yolo_rejection_total", "YOLO produced no detection above threshold")
triton_batch_size = Histogram("ocr_triton_batch_size", "Triton-reported batch size per response")

# ---- Gemini ----
gemini_retries_total = Counter("ocr_gemini_retries_total", "Gemini 5xx/timeout retry attempts", ["attempt"])
gemini_tokens_total = Counter("ocr_gemini_tokens_total", "Gemini token usage", ["kind"])
llm_payload_bytes = Histogram("ocr_llm_payload_bytes", "JPEG q85 payload size sent to Gemini")

# ---- rate limit / yield ----
rate_limit_yields_total = Counter("ocr_rate_limit_yields_total", "Worker yielded to queue due to rate limit")
requeue_count = Histogram("ocr_requeue_count", "Observed requeue count per yield event")
token_bucket_acquire_total = Counter("ocr_token_bucket_acquire_total", "Token bucket try_acquire outcomes", ["outcome"])
token_bucket_refresh_total = Counter("ocr_token_bucket_refresh_total", "Rate-limit refresh tick outcomes", ["outcome"])
token_bucket_available = Gauge("ocr_token_bucket_available", "Current tokens available (sampled at refresh tick)")

# ---- inflight ----
inflight_jobs = Gauge("ocr_inflight_jobs", "Jobs currently inside execute_task_lifecycle")

# ---- whitelist (frozen labels) ----
whitelist_match_total = Counter("ocr_whitelist_match_total", "Whitelist match outcomes", ["field", "tier"])

# ---- sweeper / orphan ----
stale_jobs_recovered_total = Counter(
    "ocr_stale_jobs_recovered_total", "Stale jobs reclaimed by sweeper", ["from_status"]
)
orphan_jobs_total = Counter("ocr_orphan_jobs_total", "Pops with no matching Postgres row")


def start_metrics_server(port: int) -> None:
    start_http_server(port)
