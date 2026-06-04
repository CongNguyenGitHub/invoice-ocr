"""Init container — alembic upgrade head + readiness probes.

Exits non-zero on any failure so docker-compose's
`depends_on: service_completed_successfully` gates api/worker correctly.
"""
from __future__ import annotations

import asyncio
import logging
import sys

from src.config import settings
from src.logging_config import configure_logging
from src.storage.postgres_client import get_pg
from src.storage.redis_client import get_redis

logger = logging.getLogger(__name__)


def _run_alembic_upgrade() -> None:
    """Synchronous — alembic uses sync SQLAlchemy."""
    from alembic import command
    from alembic.config import Config

    cfg = Config("migrations/alembic.ini")
    dsn = settings.POSTGRES_DSN.replace("+asyncpg", "+psycopg2")
    cfg.set_main_option("sqlalchemy.url", dsn)
    command.upgrade(cfg, "head")


async def main() -> int:
    configure_logging(service="init")
    try:
        # 1. DB migrations
        logger.info("init_alembic_upgrade_start")
        await asyncio.to_thread(_run_alembic_upgrade)
        logger.info("init_alembic_upgrade_done")

        # 2. Readiness probes — catch config errors before releasing the gate
        pg = get_pg()
        redis = get_redis()
        await pg.init_pool()
        await redis.init()
        assert await pg.ping(), "pg ping failed"
        assert await redis.ping(), "redis ping failed"
        await pg.close_pool()
        await redis.close()
        logger.info("init_done")
        return 0
    except Exception:
        logger.exception("init_container_failed")
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(asyncio.run(main()))
