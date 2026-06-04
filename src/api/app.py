"""FastAPI app factory."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.api.metrics import queue_depth_sampler
from src.api.routes import router
from src.config import settings
from src.domain.errors import (
    DatabaseUnavailable,
    StorageTransientError,
)
from src.logging_config import configure_logging
from src.storage.postgres_client import pg
from src.storage.redis_client import redis
from src.worker.metrics import start_metrics_server  # API serves metrics on its own port

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    configure_logging("api")
    logger.info("api_boot")

    await pg.init_pool()
    await redis.init()

    import asyncio

    # Metrics on a SEPARATE port (I16: API=9101, worker=9102)
    start_metrics_server(settings.API_METRICS_PORT)

    sampler = asyncio.create_task(queue_depth_sampler())
    try:
        yield
    finally:
        sampler.cancel()
        try:
            await sampler
        except asyncio.CancelledError:
            pass
        await redis.close()
        await pg.close_pool()
        logger.info("api_exit")


def create_app() -> FastAPI:
    app = FastAPI(title="Invoice OCR v3", version="3.0.0", lifespan=_lifespan)
    app.include_router(router)
    _register_exception_handlers(app)
    return app


def _register_exception_handlers(app: FastAPI) -> None:
    """Domain error → HTTP status mapping.

    Handlers:
      * DatabaseUnavailable     → 503
      * StorageTransientError   → 503
      * Exception (fallback)    → 500
    """
    from fastapi.responses import JSONResponse

    def _envelope(code: str, msg: str, permanent: bool) -> dict:
        return {
            "status": "FAILED_PERMANENT" if permanent else "FAILED_TRANSIENT",
            "error_code": code,
            "error_message": msg,
        }

    @app.exception_handler(DatabaseUnavailable)
    async def _h_db(request, exc: DatabaseUnavailable):  # noqa: ANN001
        return JSONResponse(_envelope(exc.error_code, str(exc), False), status_code=503)

    @app.exception_handler(StorageTransientError)
    async def _h_storage(request, exc: StorageTransientError):  # noqa: ANN001
        return JSONResponse(_envelope(exc.error_code, str(exc), False), status_code=503)

    @app.exception_handler(Exception)
    async def _h_fallback(request, exc: Exception):  # noqa: ANN001
        logger.exception("api_uncaught", extra={"err": str(exc)})
        return JSONResponse(_envelope("internal_error", str(exc), False), status_code=500)


app = create_app()
