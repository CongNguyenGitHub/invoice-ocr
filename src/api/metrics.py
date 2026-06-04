"""API-side Prometheus metrics + queue-depth sampler.

Binds to a DIFFERENT port than worker metrics (9101 vs 9102) — arch §13,
invariant I16.
"""
from __future__ import annotations

import asyncio
import logging

from prometheus_client import Counter, Gauge, Histogram, start_http_server

logger = logging.getLogger(__name__)

requests_total = Counter(
    "ocr_requests_total", "Terminal outcome of submit_receipt",
    ["status"],  # success / pipeline_failed / timeout / storage_error / backpressure
)
e2e_latency_seconds = Histogram(
    "ocr_e2e_latency_seconds", "End-to-end submit_receipt latency (try/finally)"
)
queue_depth = Gauge("ocr_queue_depth", "LLEN ocr:queue, sampled every 5 s")
queue_soft_warn_total = Counter(
    "ocr_queue_soft_warn_total", "Queue exceeded soft-warn threshold"
)
backpressure_rejections_total = Counter(
    "ocr_backpressure_rejections_total", "HTTP 429 responses from check_backpressure"
)


def start_metrics_server_api(port: int) -> None:
    start_http_server(port)


async def queue_depth_sampler() -> None:
    """Update ocr_queue_depth every 5 s. Cancelled on shutdown."""
    from src.storage.redis_client import get_redis

    r = get_redis()
    while True:
        try:
            queue_depth.set(await r.get_queue_depth())
        except Exception:  # noqa: BLE001
            logger.exception("queue_depth_sampler_failed")
        await asyncio.sleep(5)
