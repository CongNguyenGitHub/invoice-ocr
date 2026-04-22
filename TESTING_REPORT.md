# Invoice OCR Testing Infrastructure Report

Generated: 2026-04-21

## Unit Tests (31 total)

### test_m0_skeleton.py (6 tests)
- test_settings_import
- test_redis_key_helpers_contain_psv
- test_redis_result_key_format
- test_prompt_file_path_points_into_pipeline_prompts
- test_schema_all_strings_missing_empty
- test_schema_extra_forbid
- test_error_hierarchy_exposes_error_code
- test_jobstatus_enum_values

### test_m1_storage_surface.py (4 tests)
- test_minio_client_surface
- test_postgres_client_surface
- test_redis_client_surface
- test_alembic_migration_module_imports

### test_m2_pipeline_core.py (10 tests)
- test_preprocess_returns_two_field_result_with_phash
- test_preprocess_resizes_oversized
- test_preprocess_truncated_raises_permanent
- test_normalize_money_vietnamese
- test_normalize_date_fans_in
- test_normalize_quantity_collapses_integers
- test_whitelist_match_store_exact_and_fuzzy
- test_whitelist_product_no_fallback_returns_nfc_raw
- test_whitelist_reload_swaps_atomically
- test_legacy_json_schema_strict

### test_m3_detector.py (2 async tests)
- test_detector_crops_highest_confidence_box
- test_detector_rejects_low_confidence

### test_m4_extractor.py (5 async tests)
- test_extractor_returns_validated_invoice
- test_extractor_429_yields_rate_limited
- test_extractor_5xx_retries_then_exhausts
- test_extractor_prompt_missing_raises_on_first_call
- test_extractor_invalid_json_is_permanent

### test_m5_api_surface.py (4 async tests)
- test_render_payload_success_returns_bare_invoice
- test_render_payload_permanent_returns_422
- test_render_payload_transient_returns_503
- test_check_backpressure_rejects_at_threshold
- test_check_backpressure_passes_under_warn
- test_token_bucket_acquires_and_denies

## Evaluation Scripts

### scripts/run_eval.py (528 lines)
- HTTP-based batch evaluation against live API
- Downloads images, submits to POST /v1/receipts
- Handles polling for async results (202, 504)
- Concurrent execution (default 4 workers)
- Field accuracy aggregation (global + by store type)

### scripts/run_experiment.py (327 lines)
- Prompt tuning harness with 3-run budget
- Tracks runs in experiments/ directory
- Locks test set after final eval
- Generates comparison markdown table

## Load Testing

### scripts/load_test.py (424 lines)
- Step-load: Phase 1 (1.5 RPS x 60s), Phase 2 (3.0 RPS x 120s)
- SLA assertions: p95<10s, p99<30s, err<1%
- Returns exit code 0 (pass) or 1 (breach)
- JSON report with per-phase statistics

### scripts/ci_load_test.sh (130 lines)
- Full CI gate: Start Docker → wait /readyz → load test → teardown
- Exit code propagates from load_test.py
- Supports SKIP_DOCKER environment variable

## API Endpoints

### POST /v1/receipts
- 200: Success (bare InvoiceResult)
- 202: Pending (PendingEnvelope with job_id)
- 413: PayloadTooLarge
- 415: UnsupportedMediaType
- 429: Backpressure
- 503: StorageTransient/DatabaseUnavailable
- 504: Gateway Timeout
- 500: Internal error

### GET /v1/receipts/{job_id}
- 200: SUCCEEDED
- 202: PENDING/PROCESSING
- 404: Not found
- 422: FAILED_PERMANENT
- 503: FAILED_TRANSIENT

### GET /healthz
- Always 200: {"status": "ok"}

### GET /readyz
- Probes: redis, postgres, minio, triton
- Returns 200 with component status (check ready field)

## Docker Services (9 total)

- init (1x) — Alembic migrations
- api (1x) — FastAPI on port 8000
- worker (4x) — Job processing, 4 async tasks each
- triton (1x) — YOLO inference on port 8001
- redis (1x) — Queue + cache on port 6379
- postgres (1x) — Job records on port 5432
- minio (1x) — Blob storage on port 9000
- prometheus (1x) — Metrics on port 9090
- grafana (1x) — Dashboards on port 3000

## Test Dependencies (pyproject.toml)

- pytest>=8.0.0
- pytest-asyncio>=0.23.0
- httpx>=0.27.0
- ruff>=0.5.0
- mypy>=1.10.0

pytest config:
- asyncio_mode = "auto"
- testpaths = ["tests"]

## Summary

- Total unit tests: 31
- Test runtime: ~7 seconds (no Docker)
- Test libraries: pytest, pytest-asyncio, httpx, Pillow, numpy
- No GitHub Actions/GitLab CI currently configured
- API has 4 endpoints with comprehensive error handling
- Load test validates SLA against production-like traffic
- Experiment harness enables prompt tuning with budget constraints
