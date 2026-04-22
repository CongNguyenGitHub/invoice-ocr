"""Backpressure check — fast LLEN sample against REDIS_QUEUE_KEY.

Soft warn at BACKPRESSURE_QUEUE_WARN; hard reject (HTTP 429) at
BACKPRESSURE_QUEUE_REJECT. Implemented in M7 — minimal stub here so
api.routes.submit_receipt can import.
"""
from __future__ import annotations

import logging

from fastapi import HTTPException

from src.api.metrics import (
    backpressure_rejections_total,
    queue_soft_warn_total,
)
from src.config import settings
from src.storage.redis_client import redis

logger = logging.getLogger(__name__)


async def check_backpressure() -> None:
    try:
        depth = await redis.get_queue_depth()
    except Exception:  # noqa: BLE001 — backpressure check must not 5xx its own request
        return
    if depth >= settings.BACKPRESSURE_QUEUE_REJECT:
        backpressure_rejections_total.inc()
        raise HTTPException(
            status_code=429,
            detail={"error_code": "backpressure_reject", "queue_depth": depth},
            headers={"Retry-After": "5"},
        )
    if depth >= settings.BACKPRESSURE_QUEUE_WARN:
        queue_soft_warn_total.inc()
        logger.warning("queue_soft_warn", extra={"depth": depth})
