"""Redis client (redis.asyncio pool). All async.

Result channel is a LIST consumed via BLPOP — survives the publish-before-
subscribe race that pub/sub would expose. TTL 90 s auto-evicts unclaimed
results (decision #4).
"""
from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

import redis.asyncio as redis_async
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError, TimeoutError as RedisTimeoutError

from src.config import settings
from src.domain.constants import (
    REDIS_RATE_LIMIT_HASH,
    REDIS_REQUEUE_FIELD,
    REDIS_REQUEUE_HASH_FMT,
)
from src.domain.errors import StorageTransientError

logger = logging.getLogger(__name__)


class RedisClient:
    def __init__(self) -> None:
        self._client: redis_async.Redis | None = None

    async def init(self) -> None:
        self._client = redis_async.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            health_check_interval=30,
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    @property
    def _r(self) -> redis_async.Redis:
        assert self._client is not None, "RedisClient.init() not called"
        return self._client

    async def ping(self) -> bool:
        try:
            return bool(await self._r.ping())
        except RedisError:
            return False

    # ---- queue ----
    async def push_to_queue(self, job_id: UUID) -> None:
        try:
            await self._r.lpush(settings.REDIS_QUEUE_KEY, str(job_id))
        except (RedisConnectionError, RedisTimeoutError, RedisError) as e:
            raise StorageTransientError(f"redis push_to_queue failed: {e}") from e

    async def pop_from_queue(self, timeout: int = 5) -> UUID | None:
        try:
            res = await self._r.brpop(settings.REDIS_QUEUE_KEY, timeout=timeout)
        except (RedisConnectionError, RedisTimeoutError) as e:
            logger.warning("brpop_blip", extra={"err": str(e)})
            return None
        except RedisError as e:
            raise StorageTransientError(f"redis pop_from_queue failed: {e}") from e
        if not res:
            return None
        _, val = res
        try:
            return UUID(val)
        except ValueError:
            logger.error("queue_bad_uuid", extra={"val": val})
            return None

    async def get_queue_depth(self) -> int:
        try:
            return int(await self._r.llen(settings.REDIS_QUEUE_KEY))
        except RedisError as e:
            raise StorageTransientError(f"redis llen failed: {e}") from e

    # ---- result channel (LIST + BLPOP RPC) ----
    async def publish_result(self, job_id: UUID, payload: dict) -> None:
        key = settings.redis_result_key(job_id)
        body = json.dumps(payload, default=str)
        try:
            async with self._r.pipeline(transaction=False) as pipe:
                pipe.lpush(key, body)
                pipe.expire(key, settings.REDIS_RESULT_TTL_SECONDS)
                await pipe.execute()
        except (RedisConnectionError, RedisTimeoutError, RedisError) as e:
            raise StorageTransientError(f"redis publish_result failed: {e}") from e

    async def wait_for_result(self, job_id: UUID, timeout: int) -> dict | None:
        """BLPOP the per-job result LIST. Decision #38: a Redis blip during
        BLPOP must NOT 5xx an in-flight job — log + drop, return None so the
        API route falls through to the 504+poll branch."""
        key = settings.redis_result_key(job_id)
        try:
            res = await self._r.blpop(key, timeout=timeout)
        except (RedisConnectionError, RedisTimeoutError) as e:
            logger.warning("wait_for_result_blip",
                           extra={"job_id": str(job_id), "err": str(e)})
            try:
                # Best-effort metric — import lazily to avoid import-cycle on api.
                from src.api.metrics import wait_redis_drops_total
                wait_redis_drops_total.inc()
            except Exception:  # noqa: BLE001
                pass
            return None
        except RedisError as e:
            raise StorageTransientError(f"redis blpop failed: {e}") from e
        if not res:
            return None
        _, val = res
        try:
            return json.loads(val)
        except (TypeError, ValueError):
            logger.error("wait_for_result_bad_json",
                         extra={"job_id": str(job_id), "val": val})
            return None

    # ---- pHash cache (raw extraction, PSV-versioned) ----
    async def get_phash_cache(self, phash: str) -> dict | None:
        key = settings.phash_cache_key(phash)
        try:
            val = await self._r.get(key)
        except RedisError as e:
            raise StorageTransientError(f"redis get_phash_cache failed: {e}") from e
        if val is None:
            return None
        try:
            return json.loads(val)
        except (TypeError, ValueError):
            return None

    async def set_phash_cache(self, phash: str, raw_payload: dict) -> None:
        key = settings.phash_cache_key(phash)
        try:
            await self._r.setex(
                key, settings.REDIS_PHASH_TTL_SECONDS,
                json.dumps(raw_payload, default=str),
            )
        except RedisError as e:
            raise StorageTransientError(f"redis set_phash_cache failed: {e}") from e

    # ---- bounded requeue counter ----
    async def bump_requeue_counter(self, job_id: UUID) -> int:
        key = REDIS_REQUEUE_HASH_FMT.format(job_id=job_id)
        try:
            count = int(await self._r.hincrby(key, REDIS_REQUEUE_FIELD, 1))
            if count == 1:
                await self._r.expire(key, settings.REDIS_REQUEUE_TTL_SECONDS)
        except RedisError as e:
            raise StorageTransientError(f"redis bump_requeue failed: {e}") from e
        return count

    # ---- live rate-limit config ----
    async def read_rate_limit_config(self) -> dict[str, Any]:
        try:
            cfg = await self._r.hgetall(REDIS_RATE_LIMIT_HASH)
        except RedisError as e:
            raise StorageTransientError(f"redis hgetall rate_limit failed: {e}") from e
        if not cfg:
            return {
                "rps": settings.TOKEN_BUCKET_RPS,
                "burst": settings.TOKEN_BUCKET_BURST,
            }
        return {
            "rps": float(cfg.get("rps", settings.TOKEN_BUCKET_RPS)),
            "burst": int(cfg.get("burst", settings.TOKEN_BUCKET_BURST)),
        }


_singleton: RedisClient | None = None


def get_redis() -> RedisClient:
    global _singleton
    if _singleton is None:
        _singleton = RedisClient()
    return _singleton


class _LazyRedis:
    def __getattr__(self, item):  # noqa: ANN001
        return getattr(get_redis(), item)


redis = _LazyRedis()
