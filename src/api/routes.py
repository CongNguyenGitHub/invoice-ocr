"""API routes — fire-and-forget receipt endpoint with CDN URL.

Contract:
  * POST /v1/receipts — 202 Accepted (fire-and-forget)
                        | 400 bad URL / disallowed domain
                        | 429 backpressure (+ Retry-After)
                        | 503 StorageTransient
  * GET  /v1/receipts/{id} — STATUS-MIRRORING codes:
                        200 bare InvoiceResult on SUCCEEDED
                        202 pending envelope on PENDING/PROCESSING
                        422 error envelope on FAILED_PERMANENT
                        503 error envelope on FAILED_TRANSIENT
                        404 if job_id unknown
  * /healthz — static 200
  * /readyz  — aggregates redis + pg + triton probes
"""
from __future__ import annotations

import logging
import uuid
from urllib.parse import urlparse
from uuid import UUID

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from src.api.backpressure import check_backpressure
from src.api.metrics import e2e_latency_seconds, requests_total
from src.config import settings
from src.domain.constants import JobStatus
from src.domain.errors import (
    DatabaseUnavailable,
    StorageTransientError,
)
from src.logging_config import job_id_var
from src.schemas import SubmitRequest, SubmitResponse
from src.storage.postgres_client import pg
from src.storage.redis_client import redis

logger = logging.getLogger(__name__)

router = APIRouter()


def _validate_image_domain(url: str) -> None:
    """Raise ValueError if the URL domain is not in the allowlist."""
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    if not any(
        hostname == domain or hostname.endswith(f".{domain}")
        for domain in settings.ALLOWED_IMAGE_DOMAINS
    ):
        raise ValueError(
            f"Domain {hostname!r} not in allowlist: {settings.ALLOWED_IMAGE_DOMAINS}"
        )


def _pending(job_id: UUID, status_val: str, msg: str) -> dict:
    return {"job_id": str(job_id), "status": status_val, "message": msg}


@router.post("/v1/receipts", status_code=202)
async def submit_receipt(body: SubmitRequest) -> JSONResponse:
    """Fire-and-forget: validate URL, enqueue, return 202 immediately."""
    job_id = uuid.uuid4()
    token = job_id_var.set(str(job_id))
    image_url = str(body.image_url)
    with e2e_latency_seconds.time():
        try:
            # Backpressure first
            await check_backpressure()

            # Domain allowlist validation
            try:
                _validate_image_domain(image_url)
            except ValueError as e:
                requests_total.labels(status="400").inc()
                return _error_response(job_id, 400, "disallowed_domain", str(e))

            # Persist job row
            try:
                await pg.create_job_record(job_id, image_url)
            except DatabaseUnavailable as e:
                requests_total.labels(status="503").inc()
                return _error_response(job_id, 503, "database_unavailable", str(e))

            # Enqueue enriched JSON message
            try:
                await redis.push_to_queue(job_id, image_url)
            except StorageTransientError as e:
                requests_total.labels(status="503").inc()
                return _error_response(job_id, 503, "storage_transient", str(e))

            # Return 202 immediately
            requests_total.labels(status="202").inc()
            resp = SubmitResponse(
                job_id=str(job_id),
                message="Job accepted. Poll GET /v1/receipts/{job_id} for results.",
            )
            return JSONResponse(resp.model_dump(), status_code=202)

        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001 — last-ditch
            logger.exception("submit_receipt_uncaught", extra={"job_id": str(job_id)})
            requests_total.labels(status="500").inc()
            return _error_response(job_id, 500, "internal_error", str(e))
        finally:
            job_id_var.reset(token)


def _error_response(job_id: UUID, http_status: int, code: str, msg: str) -> JSONResponse:
    # Map HTTP → payload status for the envelope
    payload_status = "FAILED_PERMANENT" if http_status in (400, 413, 415, 422) else "FAILED_TRANSIENT"
    payload = {
        "job_id": str(job_id), "status": payload_status,
        "error_code": code, "error_message": msg,
    }
    return JSONResponse(payload, status_code=http_status)


# -------------------- GET /v1/receipts/{id} --------------------
@router.get("/v1/receipts/{job_id}")
async def get_receipt(job_id: UUID) -> JSONResponse:
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
        return JSONResponse(env, status_code=202)
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
async def readyz() -> JSONResponse:
    import asyncio

    from src.pipeline.triton_client import is_ready as triton_ready

    results = await asyncio.gather(
        redis.ping(), pg.ping(),
        triton_ready(),
        return_exceptions=True,
    )
    redis_ok, pg_ok, triton_ok = [
        (r is True) for r in results
    ]
    ready = all([redis_ok, pg_ok, triton_ok])
    body = {
        "ready": ready,
        "redis": redis_ok,
        "postgres": pg_ok,
        "triton": triton_ok,
    }
    return JSONResponse(body, status_code=200 if ready else 503)
