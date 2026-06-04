"""Postgres client (asyncpg pool). All async.

Every method is wrapped via @_wrap_pg_errors → asyncpg.PostgresError mapped
to DatabaseUnavailable so the worker except-tuple is meaningful.
"""
from __future__ import annotations

import functools
import json
import logging
from datetime import timedelta
from typing import Any
from uuid import UUID

import asyncpg

from src.config import settings
from src.domain.constants import JobStatus
from src.domain.errors import DatabaseUnavailable
from src.schemas import JobRecord

logger = logging.getLogger(__name__)


def _wrap_pg_errors(fn):
    """Map asyncpg connection/runtime errors to DatabaseUnavailable."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError) as e:
            raise DatabaseUnavailable(str(e)) from e

    return wrapper


def _row_to_record(row: asyncpg.Record) -> JobRecord:
    result = row["result"]
    if isinstance(result, str):
        result = json.loads(result)
    return JobRecord(
        job_id=row["job_id"],
        status=JobStatus(row["status"]),
        phash=row["phash"],
        image_url=row["image_url"],
        result=result,
        error_code=row["error_code"],
        error_message=row["error_message"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class PostgresClient:
    def __init__(self) -> None:
        self._pool: asyncpg.Pool | None = None

    # ---- pool lifecycle ----
    async def init_pool(self) -> None:
        # Strip SQLAlchemy driver suffix for asyncpg.
        dsn = settings.POSTGRES_DSN.replace("+asyncpg", "")
        try:
            self._pool = await asyncpg.create_pool(dsn, min_size=2, max_size=10)
        except (asyncpg.PostgresError, OSError) as e:
            raise DatabaseUnavailable(str(e)) from e

    async def close_pool(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @_wrap_pg_errors
    async def ping(self) -> bool:
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.execute("SELECT 1")
        return True

    # ---- per-job ops ----
    @_wrap_pg_errors
    async def create_job_record(
        self, job_id: UUID, image_url: str
    ) -> None:
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jobs (job_id, status, image_url)
                VALUES ($1, $2, $3)
                """,
                job_id,
                JobStatus.PENDING.value,
                image_url,
            )

    @_wrap_pg_errors
    async def update_job_status(
        self,
        job_id: UUID,
        status: JobStatus,
        *,
        result: dict | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        phash: str | None = None,
    ) -> None:
        sets: list[str] = ["status = $2", "updated_at = now()"]
        params: list[Any] = [job_id, status.value]
        idx = 3

        if result is not None:
            sets.append(f"result = ${idx}::jsonb")
            params.append(json.dumps(result))
            idx += 1
        if error_code is not None:
            sets.append(f"error_code = ${idx}")
            params.append(error_code)
            idx += 1
        if error_message is not None:
            sets.append(f"error_message = ${idx}")
            params.append(error_message)
            idx += 1
        if phash is not None:
            sets.append(f"phash = ${idx}")
            params.append(phash)
            idx += 1

        sql = f"UPDATE jobs SET {', '.join(sets)} WHERE job_id = $1"
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.execute(sql, *params)

    @_wrap_pg_errors
    async def touch_updated_at(self, job_id: UUID) -> None:
        """Decision #35 — keeps live-but-throttled jobs out of the sweeper window."""
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE jobs SET updated_at = now() WHERE job_id = $1", job_id
            )

    @_wrap_pg_errors
    async def get_job_record(self, job_id: UUID) -> JobRecord | None:
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM jobs WHERE job_id = $1", job_id)
        return _row_to_record(row) if row else None

    @_wrap_pg_errors
    async def select_stale_jobs(self) -> list[JobRecord]:
        """PROCESSING > 15m OR PENDING > 30m. Drives the per-row sweeper loop."""
        assert self._pool is not None
        proc_window = timedelta(minutes=settings.STALE_PROCESSING_MINUTES)
        pend_window = timedelta(minutes=settings.STALE_PENDING_MINUTES)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM jobs
                 WHERE (status = 'PROCESSING' AND updated_at < now() - $1::interval)
                    OR (status = 'PENDING'    AND created_at < now() - $2::interval)
                """,
                proc_window,
                pend_window,
            )
        return [_row_to_record(r) for r in rows]

    @_wrap_pg_errors
    async def purge_old_job_records(self) -> int:
        assert self._pool is not None
        ret_window = timedelta(days=settings.JOB_RETENTION_DAYS)
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                WITH deleted AS (
                    DELETE FROM jobs
                     WHERE status IN ('SUCCEEDED','FAILED_PERMANENT','FAILED_TRANSIENT')
                       AND updated_at < now() - $1::interval
                     RETURNING 1
                )
                SELECT count(*) AS n FROM deleted
                """,
                ret_window,
            )
        return int(row["n"]) if row else 0


_singleton: PostgresClient | None = None


def get_pg() -> PostgresClient:
    global _singleton
    if _singleton is None:
        _singleton = PostgresClient()
    return _singleton


class _LazyPg:
    def __getattr__(self, item):  # noqa: ANN001
        return getattr(get_pg(), item)


pg = _LazyPg()
