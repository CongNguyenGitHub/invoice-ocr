"""Worker entrypoint — N async tasks consuming from one Redis queue.

Started as a CMD'd container; all daemons (sweeper, rate_refresh,
nightly_purge) run inside the same process. WORKER_CONCURRENCY
governs the number of concurrent in-flight jobs per process.
"""
from __future__ import annotations

import asyncio
import logging
import signal
from typing import Awaitable, Callable
from uuid import UUID

from src.config import settings
from src.logging_config import configure_logging
from src.pipeline.whitelist_index import WhitelistIndex
from src.storage.postgres_client import pg
from src.storage.redis_client import redis
from src.utils.token_bucket import TokenBucket
from src.worker.loop import execute_task_lifecycle
from src.worker.metrics import start_metrics_server

logger = logging.getLogger(__name__)


_shutdown = asyncio.Event()


def _install_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    def _stop(signame: str) -> None:
        logger.info("shutdown_signal", extra={"signal": signame})
        _shutdown.set()

    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(s, _stop, s.name)
        except NotImplementedError:  # Windows
            signal.signal(s, lambda *_: _shutdown.set())


async def _worker_task(
    name: str,
    bucket: TokenBucket,
    index: WhitelistIndex,
) -> None:
    logger.info("worker_task_started", extra={"task": name})
    while not _shutdown.is_set():
        msg = await redis.pop_from_queue(timeout=5)
        if msg is None:
            continue
        try:
            job_id = UUID(msg["job_id"])
            image_url = msg["image_url"]
            await execute_task_lifecycle(
                job_id, image_url=image_url, bucket=bucket, index=index
            )
        except Exception:  # noqa: BLE001 — defence in depth
            logger.exception("worker_task_uncaught", extra={"task": name})
    logger.info("worker_task_stopped", extra={"task": name})


async def _run() -> int:
    configure_logging("worker")
    logger.info("worker_boot",
                extra={"worker_id": settings.WORKER_ID,
                       "concurrency": settings.WORKER_CONCURRENCY})

    await pg.init_pool()
    await redis.init()

    start_metrics_server(settings.WORKER_METRICS_PORT)

    bucket = TokenBucket(settings.TOKEN_BUCKET_RPS, settings.TOKEN_BUCKET_BURST)
    index = WhitelistIndex.build(settings.WHITELIST_DIR)

    loop = asyncio.get_running_loop()
    _install_signal_handlers(loop)

    # Daemons (registered in M6 — keep registration here)
    daemons: list[Callable[[], Awaitable[None]]] = []
    try:
        from src.worker.nightly_purge import nightly_purge_loop
        from src.worker.rate_refresh import rate_refresh_loop
        from src.worker.sweeper import sweeper_loop
        daemons.extend([
            lambda: sweeper_loop(_shutdown),
            lambda: rate_refresh_loop(_shutdown, bucket),
            lambda: nightly_purge_loop(_shutdown),
        ])
    except ImportError:
        logger.info("daemons_not_yet_implemented")

    tasks = [
        asyncio.create_task(_worker_task(f"w{i}", bucket, index))
        for i in range(settings.WORKER_CONCURRENCY)
    ]
    daemon_tasks = [asyncio.create_task(d()) for d in daemons]

    await _shutdown.wait()
    logger.info("worker_draining")
    await asyncio.gather(*tasks, return_exceptions=True)
    for dt in daemon_tasks:
        dt.cancel()
    await asyncio.gather(*daemon_tasks, return_exceptions=True)

    await pg.close_pool()
    await redis.close()
    logger.info("worker_exit_clean")
    return 0


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
