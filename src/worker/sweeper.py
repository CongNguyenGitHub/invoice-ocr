"""Stale-job sweeper. Runs every SWEEP_INTERVAL_SECONDS in every worker process.

Per-row processing:
  * Selects PROCESSING > 15m OR PENDING > 30m.
  * For each row, calls _fail (FAILED_TRANSIENT, error_code='stale_timeout').
  * _fail is never-raise and updates Postgres — so poll clients see 503.

Multiple workers running this loop is fine: _fail is idempotent (status
already terminal → pg update is a no-op write).
"""

from __future__ import annotations

import asyncio
import logging

from src.config import settings
from src.storage.postgres_client import pg
from src.worker.loop import _fail
from src.worker.metrics import stale_jobs_recovered_total

logger = logging.getLogger(__name__)


async def _sweep_once() -> int:
    rows = await pg.select_stale_jobs()
    n = 0
    for r in rows:
        try:
            await _fail(
                r.job_id,
                is_permanent=False,
                error_code="stale_timeout",
                error_message=f"sweeper: status={r.status.value} age exceeded threshold",
            )
            stale_jobs_recovered_total.labels(from_status=r.status.value).inc()
            n += 1
        except Exception:  # noqa: BLE001 — defence in depth (_fail itself is never-raise)
            logger.exception("sweeper_fail_uncaught", extra={"job_id": str(r.job_id)})
    if n:
        logger.info("sweeper_recovered", extra={"count": n})
    return n


async def sweeper_loop(shutdown: asyncio.Event) -> None:
    logger.info("sweeper_started", extra={"interval_s": settings.SWEEP_INTERVAL_SECONDS})
    while not shutdown.is_set():
        try:
            await _sweep_once()
        except Exception:  # noqa: BLE001
            logger.exception("sweeper_iter_uncaught")
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=settings.SWEEP_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass
    logger.info("sweeper_stopped")
