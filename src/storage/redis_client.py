"""Redis client (redis.asyncio pool). All async.

Queue messages are JSON-encoded dicts with job_id and image_url.
pHash cache stores raw extraction results keyed by perceptual hash.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

import redis.asyncio as redis_async
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError

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
    async def push_to_queue(self, job_id: UUID, image_url: str) -> None:
        """Push an enriched JSON message to the job queue."""
        msg = json.dumps({"job_id": str(job_id), "image_url": image_url})
        try:
            await self._r.lpush(settings.REDIS_QUEUE_KEY, msg)  # type: ignore[misc]
        except (RedisConnectionError, RedisTimeoutError, RedisError) as e:
            raise StorageTransientError(f"redis push_to_queue failed: {e}") from e

    async def pop_from_queue(self, timeout: int = 5) -> dict | None:
        """Pop an enriched JSON message from the job queue.

        Returns dict with 'job_id' and 'image_url' keys, or None on timeout.
        """
        try:
            res = await self._r.brpop([settings.REDIS_QUEUE_KEY], timeout=timeout)  # type: ignore[misc]
        except (RedisConnectionError, RedisTimeoutError) as e:
            logger.warning("brpop_blip", extra={"err": str(e)})
            return None
        except RedisError as e:
            raise StorageTransientError(f"redis pop_from_queue failed: {e}") from e
        if not res:
            return None
        _, val = res
        try:
            return json.loads(val)
        except (TypeError, ValueError):
            logger.error("queue_bad_json", extra={"val": val})
            return None

    async def get_queue_depth(self) -> int:
        try:
            return int(await self._r.llen(settings.REDIS_QUEUE_KEY))  # type: ignore[misc]
        except RedisError as e:
            raise StorageTransientError(f"redis llen failed: {e}") from e

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
                key,
                settings.REDIS_PHASH_TTL_SECONDS,
                json.dumps(raw_payload, default=str),
            )
        except RedisError as e:
            raise StorageTransientError(f"redis set_phash_cache failed: {e}") from e

    # ---- bounded requeue counter ----
    async def bump_requeue_counter(self, job_id: UUID) -> int:
        key = REDIS_REQUEUE_HASH_FMT.format(job_id=job_id)
        try:
            count = int(await self._r.hincrby(key, REDIS_REQUEUE_FIELD, 1))  # type: ignore[misc]
            if count == 1:
                await self._r.expire(key, settings.REDIS_REQUEUE_TTL_SECONDS)
        except RedisError as e:
            raise StorageTransientError(f"redis bump_requeue failed: {e}") from e
        return count

    # ---- live rate-limit config ----
    async def read_rate_limit_config(self) -> dict[str, Any]:
        try:
            cfg = await self._r.hgetall(REDIS_RATE_LIMIT_HASH)  # type: ignore[misc]
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
