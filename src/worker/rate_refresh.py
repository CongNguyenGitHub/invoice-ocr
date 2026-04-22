"""Rate-limit refresh daemon — polls Redis rate_limit_config every N seconds."""
from __future__ import annotations

import asyncio
import logging

from src.config import settings
from src.storage.redis_client import redis
from src.utils.token_bucket import TokenBucket
from src.worker.metrics import token_bucket_refresh_total

logger = logging.getLogger(__name__)


async def rate_refresh_loop(shutdown: asyncio.Event, bucket: TokenBucket) -> None:
    logger.info("rate_refresh_started",
                extra={"interval_s": settings.RATE_LIMIT_REFRESH_INTERVAL})
    while not shutdown.is_set():
        try:
            cfg = await redis.read_rate_limit_config()
            await bucket.update_config(float(cfg["rps"]), int(cfg["burst"]))
            token_bucket_refresh_total.labels(outcome="ok").inc()
        except Exception:  # noqa: BLE001
            logger.exception("rate_refresh_failed")
            try:
                token_bucket_refresh_total.labels(outcome="error").inc()
            except Exception:  # noqa: BLE001
                pass
        try:
            await asyncio.wait_for(shutdown.wait(),
                                   timeout=settings.RATE_LIMIT_REFRESH_INTERVAL)
        except asyncio.TimeoutError:
            pass
    logger.info("rate_refresh_stopped")
