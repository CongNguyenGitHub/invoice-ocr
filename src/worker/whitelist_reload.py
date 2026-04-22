"""Whitelist hot-reload — runs in a daemon THREAD (not asyncio).

Polls mtime of each whitelist file every WHITELIST_RELOAD_INTERVAL seconds.
On change, calls WhitelistIndex.reload(kind, path) which atomically swaps
under the index's internal lock. Mirrors the asyncio shutdown event via a
threading.Event so SIGTERM cleanly stops the thread.
"""
from __future__ import annotations

import asyncio
import logging
import threading

from src.config import settings
from src.pipeline.whitelist_index import WhitelistIndex
from src.worker.metrics import whitelist_reload_failed_total, whitelist_reload_total

logger = logging.getLogger(__name__)


def _bridge_event(asyncio_evt: asyncio.Event) -> threading.Event:
    """Mirror asyncio.Event into a threading.Event so the worker thread can wait on it."""
    tev = threading.Event()

    async def _watcher():
        await asyncio_evt.wait()
        tev.set()

    try:
        loop = asyncio.get_event_loop()
        loop.create_task(_watcher())
    except RuntimeError:
        # No running loop yet — caller must arrange this
        pass
    return tev


def _watch(index: WhitelistIndex, shutdown_tevent: threading.Event) -> None:
    logger.info("whitelist_reloader_started",
                extra={"interval_s": settings.WHITELIST_RELOAD_INTERVAL})
    while not shutdown_tevent.wait(timeout=settings.WHITELIST_RELOAD_INTERVAL):
        for kind, path in list(index.source_path.items()):
            try:
                if not path.exists():
                    continue
                mtime = path.stat().st_mtime
                if mtime > index.last_mtime.get(kind, 0.0):
                    index.reload(kind, path)
                    whitelist_reload_total.labels(kind=kind).inc()
                    logger.info("whitelist_reloaded", extra={"kind": kind, "path": str(path)})
            except Exception:  # noqa: BLE001
                whitelist_reload_failed_total.labels(kind=kind).inc()
                logger.exception("whitelist_reload_failed",
                                 extra={"kind": kind, "path": str(path)})
    logger.info("whitelist_reloader_stopped")


def start_whitelist_reloader(
    index: WhitelistIndex, shutdown: asyncio.Event
) -> threading.Thread:
    tev = _bridge_event(shutdown)
    th = threading.Thread(
        target=_watch, args=(index, tev),
        name="whitelist-reloader", daemon=True,
    )
    th.start()
    return th
