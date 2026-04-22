"""API routes — synchronous receipt endpoint over Redis BLPOP RPC.

Contract (decision #27, #31):
  * POST /v1/receipts — 200 bare InvoiceResult | 202 PendingEnvelope
                        | 413 PayloadTooLarge | 415 Unsupported
                        | 422 extractor_invalid_json
                        | 429 backpressure (+ Retry-After)
                        | 503 StorageTransient | 504 PendingEnvelope (poll)
  * GET  /v1/receipts/{id} — STATUS-MIRRORING codes:
                        200 bare InvoiceResult on SUCCEEDED
                        202 PendingEnvelope on PENDING/PROCESSING
                        422 ErrorPayload on FAILED_PERMANENT
                        503 ErrorPayload on FAILED_TRANSIENT
                        404 if job_id unknown
  * /healthz — static 200
  * /readyz  — aggregates redis + pg + minio + triton probes
"""
from __future__ import annotations

import logging
import uuid
from typing import Any
from uuid import UUID

from fastapi import APIRouter, File, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import JSONResponse

from src.api.backpressure import check_backpressure
from src.api.metrics import e2e_latency_seconds, requests_total
from src.config import settings
from src.domain.constants import JobStatus
from src.domain.errors import (
    DatabaseUnavailable,
    PayloadTooLarge,
    StorageTransientError,
    UnsupportedMediaType,
)
from src.logging_config import job_id_var
from src.pipeline.preprocessor import preprocess_image
from src.schemas import ErrorPayload, InvoiceResult, PendingEnvelope
from src.storage.minio_client import minio
from src.storage.postgres_client import pg
from src.storage.redis_client import redis

logger = logging.getLogger(__name__)

router = APIRouter()

_ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp"}


def _pending(job_id: UUID, status_val: str, msg: str) -> PendingEnvelope:
    return PendingEnvelope(job_id=str(job_id), status=status_val, message=msg)  # type: ignore[arg-type]


@router.post("/v1/receipts")
async def submit_receipt(request: Request, file: UploadFile = File(...)) -> Response:
    """Synchronous ingress. Invariant I1: upload-before-enqueue."""
    job_id = uuid.uuid4()
    token = job_id_var.set(str(job_id))
    with e2e_latency_seconds.time():
        try:
            # Backpressure first (before we read file)
            await check_backpressure()

            # Content-type guard
            if file.content_type not in _ALLOWED_MIME:
                raise UnsupportedMediaType(f"got {file.content_type!r}")

            # Size guard
            raw = await file.read()
            if len(raw) > settings.API_MAX_IMAGE_BYTES:
                raise PayloadTooLarge(f"{len(raw)} > {settings.API_MAX_IMAGE_BYTES}")

            # Compute phash up-front (duplicate-cache pre-warming; also validates the image)
            try:
                pp = preprocess_image(raw)
                phash: str | None = pp.phash
            except Exception:  # noqa: BLE001 — truncated raises PermanentPipelineError
                phash = None  # defer validation to worker — can't surface here as 4xx

            # I1: upload MinIO first
            minio_key = f"{job_id}.bin"
            try:
                await _upload(minio_key, raw)
            except StorageTransientError as e:
                requests_total.labels(status="503").inc()
                return _error_response(job_id, 503, "storage_transient", str(e))

            # Persist job row
            try:
                await pg.create_job_record(job_id, minio_key, phash)
            except DatabaseUnavailable as e:
                requests_total.labels(status="503").inc()
                return _error_response(job_id, 503, "database_unavailable", str(e))

            # Enqueue
            try:
                await redis.push_to_queue(job_id)
            except StorageTransientError as e:
                requests_total.labels(status="503").inc()
                return _error_response(job_id, 503, "storage_transient", str(e))

            # BLPOP with API_TIMEOUT_SECONDS (default 60)
            payload = await redis.wait_for_result(job_id, timeout=settings.API_TIMEOUT_SECONDS)
            if payload is None:
                requests_total.labels(status="504").inc()
                env = _pending(job_id, "PROCESSING", "still in flight — poll GET /v1/receipts/{id}")
                return JSONResponse(env.model_dump(), status_code=504)

            return _render_payload(job_id, payload)

        except UnsupportedMediaType as e:
            requests_total.labels(status="415").inc()
            return _error_response(job_id, 415, e.error_code, str(e))
        except PayloadTooLarge as e:
            requests_total.labels(status="413").inc()
            return _error_response(job_id, 413, e.error_code, str(e))
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001 — last-ditch
            logger.exception("submit_receipt_uncaught", extra={"job_id": str(job_id)})
            requests_total.labels(status="500").inc()
            return _error_response(job_id, 500, "internal_error", str(e))
        finally:
            job_id_var.reset(token)


async def _upload(key: str, raw: bytes) -> None:
    import asyncio

    await asyncio.to_thread(minio.upload_file, key, raw)


def _error_response(job_id: UUID, http_status: int, code: str, msg: str) -> JSONResponse:
    # Map HTTP → payload status for the envelope (decision #27)
    payload_status = "FAILED_PERMANENT" if http_status in (413, 415, 422) else "FAILED_TRANSIENT"
    payload = ErrorPayload(
        job_id=str(job_id), status=payload_status,  # type: ignore[arg-type]
        error_code=code, error_message=msg,
    )
    return JSONResponse(payload.model_dump(), status_code=http_status)


def _render_payload(job_id: UUID, payload: dict[str, Any]) -> Response:
    """Map a Redis result payload (success or error) to an HTTP response."""
    status_val = payload.get("status")
    if status_val == "SUCCEEDED":
        result = payload.get("result", {})
        requests_total.labels(status="200").inc()
        return JSONResponse(result, status_code=200)  # bare InvoiceResult
    if status_val == "FAILED_PERMANENT":
        http = 422
    elif status_val == "FAILED_TRANSIENT":
        http = 503
    else:
        http = 500
    requests_total.labels(status=str(http)).inc()
    return JSONResponse(payload, status_code=http)


# -------------------- GET /v1/receipts/{id} --------------------
@router.get("/v1/receipts/{job_id}")
async def get_receipt(job_id: UUID) -> Response:
    try:
        record = await pg.get_job_record(job_id)
    except DatabaseUnavailable as e:
        return _error_response(job_id, 503, "database_unavailable", str(e))

    if record is None:
        raise HTTPException(status_code=404, detail={"error_code": "job_not_found"})

    s = record.status
    if s == JobStatus.SUCCEEDED:
        return JSONResponse(record.result or {}, status_code=200)
    if s in (JobStatus.PENDING, JobStatus.PROCESSING):
        env = _pending(job_id, s.value, "still in flight")
        return JSONResponse(env.model_dump(), status_code=202)
    if s == JobStatus.FAILED_PERMANENT:
        return _error_response(job_id, 422, record.error_code or "failed_permanent",
                               record.error_message or "")
    if s == JobStatus.FAILED_TRANSIENT:
        return _error_response(job_id, 503, record.error_code or "failed_transient",
                               record.error_message or "")
    raise HTTPException(status_code=500, detail={"error_code": "bad_status",
                                                  "status": s.value})


# -------------------- health --------------------
@router.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz() -> Response:
    import asyncio as _asyncio
    from src.pipeline.triton_client import is_ready as triton_ready

    results = await _asyncio.gather(
        redis.ping(), pg.ping(),
        _asyncio.to_thread(minio.head_bucket),
        triton_ready(),
        return_exceptions=True,
    )
    redis_ok, pg_ok, minio_ok, triton_ok = [
        (r is True) for r in results
    ]
    ready = all([redis_ok, pg_ok, minio_ok, triton_ok])
    body = {
        "ready": ready,
        "redis": redis_ok,
        "postgres": pg_ok,
        "minio": minio_ok,
        "triton": triton_ok,
    }
    return JSONResponse(body, status_code=200 if ready else 503)
