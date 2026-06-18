"""Worker async loop — single in-flight job per task slot.

`execute_task_lifecycle` is the canonical state machine; it is the ONLY place
that transitions a job to a terminal state.

Invariants enforced here:
  * I3 no time.sleep in asyncio: all waits are awaitable.
  * I5 raw cache, always-postprocess: cache stores RAW (pre-postprocess) dump;
    postprocess always runs, including on cache hits.
  * I6 PSV in cache key: handled by settings.phash_cache_key().
  * I8 bounded requeue: HINCRBY counter caps at REQUEUE_MAX (default 3).
  * I12 orphans dropped: rows past requeue cap → _orphan + FAILED_PERMANENT.
  * I18 yield touches updated_at: keeps live-but-throttled jobs out of the sweeper.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING
from uuid import UUID

from src.config import settings
from src.domain.constants import JobStatus
from src.domain.errors import (
    DatabaseUnavailable,
    GeminiExhaustedError,
    OCRSystemError,
    PermanentPipelineError,
    RateLimitedLocallyError,
    StorageTransientError,
    TritonUnavailableError,
)
from src.logging_config import job_id_var
from src.pipeline.detector import detect_invoice
from src.pipeline.extractor import extract_invoice
from src.pipeline.postprocessor import postprocess
from src.pipeline.preprocessor import preprocess_image
from src.schemas import InvoiceResult
from src.storage import http_client
from src.storage.postgres_client import pg
from src.storage.redis_client import redis
from src.worker.metrics import (
    inflight_jobs,
    orphan_jobs_total,
    phash_hits,
    phash_misses,
    phash_schema_drift,
    rate_limit_yields_total,
    requeue_count,
    stage_duration_seconds,
)

if TYPE_CHECKING:
    from src.pipeline.whitelist_index import WhitelistIndex
    from src.utils.token_bucket import TokenBucket

logger = logging.getLogger(__name__)


# -------------------- terminal-state helpers --------------------
async def _fail(
    job_id: UUID,
    is_permanent: bool,
    error_code: str,
    error_message: str,
) -> None:
    """Idempotent terminal failure (never raises).

    Updates Postgres with the final error status. No MinIO operations needed
    since images live on the external CDN and Q1 decided to store URL only.
    """
    status = JobStatus.FAILED_PERMANENT if is_permanent else JobStatus.FAILED_TRANSIENT

    try:
        await pg.update_job_status(
            job_id,
            status,
            error_code=error_code,
            error_message=error_message,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception(
            "fail_pg_update",
            extra={
                "job_id": str(job_id),
                "err": str(e),
            },
        )


async def _yield_to_queue(job_id: UUID, image_url: str) -> None:
    """Push job back to the tail of the queue and touch updated_at (I18)."""
    await pg.touch_updated_at(job_id)  # decision #35 — keeps sweeper at bay
    await redis.push_to_queue(job_id, image_url)
    rate_limit_yields_total.inc()


async def _orphan(job_id: UUID) -> None:
    """Requeue cap exceeded (I12). Promote to FAILED_PERMANENT via _fail."""
    orphan_jobs_total.inc()
    await _fail(
        job_id,
        is_permanent=True,
        error_code="orphan_requeue_cap",
        error_message=f"requeue counter exceeded {settings.REQUEUE_MAX}",
    )


# -------------------- main lifecycle --------------------
async def execute_task_lifecycle(
    job_id: UUID,
    *,
    image_url: str,
    bucket: "TokenBucket",
    index: "WhitelistIndex",
) -> None:
    """One job, end-to-end. Owns ALL exception mapping for the lifecycle."""
    token = job_id_var.set(str(job_id))
    inflight_jobs.inc()
    try:
        # 0. Quick status check — skip if already terminal
        try:
            record = await pg.get_job_record(job_id)
        except DatabaseUnavailable:
            # Postgres blip — yield (don't requeue-count this; it's not the job's fault)
            await _yield_to_queue(job_id, image_url)
            return

        if record is None:
            logger.warning("job_not_in_pg", extra={"job_id": str(job_id)})
            return  # silently drop — nothing to publish to

        if record.status in (JobStatus.SUCCEEDED, JobStatus.FAILED_PERMANENT, JobStatus.FAILED_TRANSIENT):
            logger.info("job_already_terminal", extra={"job_id": str(job_id), "status": record.status.value})
            return  # already terminal — drop

        # 1. Mark PROCESSING
        try:
            await pg.update_job_status(job_id, JobStatus.PROCESSING)
        except DatabaseUnavailable:
            await _yield_to_queue(job_id, image_url)
            return

        # 2. Download image from CDN
        try:
            with stage_duration_seconds.labels(stage="download").time():
                raw = await http_client.download_image(image_url)
        except PermanentPipelineError as e:
            await _fail(job_id, is_permanent=True, error_code=e.error_code, error_message=str(e))
            return
        except StorageTransientError:
            count = await _bump_or_orphan(job_id, image_url)
            if count is None:
                return
            await _yield_to_queue(job_id, image_url)
            return

        # 3. Preprocess (CPU)
        try:
            with stage_duration_seconds.labels(stage="preprocess").time():
                pp = preprocess_image(raw)
        except PermanentPipelineError as e:
            await _fail(job_id, is_permanent=True, error_code=e.error_code, error_message=str(e))
            return

        phash = pp.phash
        if record.phash != phash:
            try:
                await pg.update_job_status(job_id, JobStatus.PROCESSING, phash=phash)
            except DatabaseUnavailable:
                pass  # non-fatal — phash will land on success update

        # 4. Cache lookup (I5/I6)
        cached_raw: dict | None = None
        try:
            cached_raw = await redis.get_phash_cache(phash)
        except StorageTransientError:
            cached_raw = None  # cache blip → treat as miss

        cached_invoice: InvoiceResult | None = None
        if cached_raw is not None:
            try:
                cached_invoice = InvoiceResult.model_validate(cached_raw)
                phash_hits.inc()
            except Exception:  # noqa: BLE001 — schema drift on cache
                phash_schema_drift.inc()
                cached_invoice = None  # fall through to extract

        if cached_invoice is None:
            phash_misses.inc()
            # 5. YOLO detect
            try:
                with stage_duration_seconds.labels(stage="detect").time():
                    crop = await detect_invoice(pp.pil)
            except PermanentPipelineError as e:
                await _fail(job_id, is_permanent=True, error_code=e.error_code, error_message=str(e))
                return
            except TritonUnavailableError:
                count = await _bump_or_orphan(job_id, image_url)
                if count is None:
                    return
                await _yield_to_queue(job_id, image_url)
                return

            # 6. Token bucket — yield if empty (I3, no sleep)
            if not await bucket.acquire():
                await _yield_to_queue(job_id, image_url)
                return

            # 7. Gemini extract
            try:
                with stage_duration_seconds.labels(stage="extract").time():
                    raw_invoice = await extract_invoice(crop)
            except RateLimitedLocallyError:
                await _yield_to_queue(job_id, image_url)
                return
            except GeminiExhaustedError:
                count = await _bump_or_orphan(job_id, image_url)
                if count is None:
                    return
                await _yield_to_queue(job_id, image_url)
                return
            except PermanentPipelineError as e:
                await _fail(job_id, is_permanent=True, error_code=e.error_code, error_message=str(e))
                return

            # 8. Cache RAW (pre-postprocess) — I5
            try:
                await redis.set_phash_cache(phash, raw_invoice.model_dump())
            except StorageTransientError:
                pass  # cache write best-effort
        else:
            raw_invoice = cached_invoice

        # 9. Postprocess (always, even on cache hit — I5 + whitelist)
        with stage_duration_seconds.labels(stage="postprocess").time():
            final = postprocess(raw_invoice, index)

        # 10. Persist result
        try:
            await pg.update_job_status(
                job_id,
                JobStatus.SUCCEEDED,
                result=final.model_dump(),
            )
        except DatabaseUnavailable:
            # Couldn't persist — yield and retry.
            await _yield_to_queue(job_id, image_url)
            return

    except OCRSystemError as e:
        # Catch-all for any uncategorized domain error — _fail is never-raise
        await _fail(
            job_id,
            is_permanent=getattr(e, "is_permanent", False),
            error_code=e.error_code,
            error_message=str(e),
        )
    except Exception as e:  # noqa: BLE001 — last-ditch
        logger.exception(
            "lifecycle_uncaught",
            extra={
                "job_id": str(job_id),
                "err": str(e),
            },
        )
        with contextlib.suppress(Exception):
            await _fail(
                job_id,
                is_permanent=False,
                error_code="lifecycle_uncaught",
                error_message=str(e),
            )
    finally:
        inflight_jobs.dec()
        job_id_var.reset(token)


async def _bump_or_orphan(
    job_id: UUID,
    image_url: str,
) -> int | None:
    """Returns the count if we should yield, or None if we orphaned."""
    try:
        count = await redis.bump_requeue_counter(job_id)
    except StorageTransientError:
        # Redis blip — best to drop and let sweeper reclaim.
        await _yield_to_queue(job_id, image_url)
        return 0
    requeue_count.observe(count)
    if count > settings.REQUEUE_MAX:
        await _orphan(job_id)
        return None
    return count
