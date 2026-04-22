"""Nightly purge — terminal-state rows older than JOB_RETENTION_DAYS.

Runs on exactly ONE worker (settings.PURGE_WORKER_ID == settings.WORKER_ID)
at local 02:00. This avoids the DELETE storm of N workers running it
simultaneously.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from src.config import settings
from src.storage.postgres_client import pg

logger = logging.getLogger(__name__)


def _seconds_until_next_0200() -> float:
    now = datetime.now()
    target = now.replace(hour=2, minute=0, second=0, microsecond=0)
    if target <= now:
        target = target.replace(day=now.day + 1) if now.day < 28 else target
        # Safer: add a day via timedelta
        from datetime import timedelta
        target = now.replace(hour=2, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return max(60.0, (target - now).total_seconds())


async def nightly_purge_loop(shutdown: asyncio.Event) -> None:
    if settings.WORKER_ID != settings.PURGE_WORKER_ID:
        logger.info("nightly_purge_skipped_non_leader",
                    extra={"worker_id": settings.WORKER_ID,
                           "purge_worker_id": settings.PURGE_WORKER_ID})
        # Idle daemon: just sit on shutdown
        await shutdown.wait()
        return

    logger.info("nightly_purge_started",
                extra={"retention_days": settings.JOB_RETENTION_DAYS})
    while not shutdown.is_set():
        wait_s = _seconds_until_next_0200()
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=wait_s)
            break
        except asyncio.TimeoutError:
            pass
        try:
            n = await pg.purge_old_job_records()
            logger.info("nightly_purge_done", extra={"deleted": n})
        except Exception:  # noqa: BLE001
            logger.exception("nightly_purge_failed")
    logger.info("nightly_purge_stopped")
