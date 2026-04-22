"""MinIO client. Blocking SDK — every method called via asyncio.to_thread.

Init container is the SOLE caller of `ensure_buckets_exist` and
`configure_lifecycles` (decision #32). API and worker call only
`assert_buckets_exist` (read-only) at startup → fail-fast.
"""
from __future__ import annotations

import io
import logging
from datetime import datetime, timezone

from minio import Minio
from minio.commonconfig import CopySource
from minio.error import S3Error
from minio.lifecycleconfig import Expiration, LifecycleConfig, Rule
from urllib3.exceptions import MaxRetryError

from src.config import settings
from src.domain.errors import ObjectNotFoundError, StorageTransientError

logger = logging.getLogger(__name__)


def _client() -> Minio:
    return Minio(
        settings.MINIO_ENDPOINT,
        access_key=settings.MINIO_ACCESS_KEY,
        secret_key=settings.MINIO_SECRET_KEY,
        secure=settings.MINIO_SECURE,
    )


class MinIOClient:
    def __init__(self) -> None:
        self._client = _client()
        self._uploads = settings.MINIO_BUCKET_UPLOADS
        self._failed = settings.MINIO_BUCKET_FAILED

    # ---- init-container only ----
    def ensure_buckets_exist(self) -> None:
        """Idempotent: create both buckets if missing. Init container only."""
        for bucket in (self._uploads, self._failed):
            try:
                if not self._client.bucket_exists(bucket):
                    self._client.make_bucket(bucket)
                    logger.info("created_bucket", extra={"bucket": bucket})
            except (S3Error, MaxRetryError, OSError) as e:
                raise StorageTransientError(f"ensure_buckets_exist failed: {e}") from e

    def configure_lifecycles(self) -> None:
        """7d on invoices/ (orphan-blob floor), 30d on failed-invoices/ (PII window).
        Init container only."""
        try:
            self._client.set_bucket_lifecycle(
                self._uploads,
                LifecycleConfig([Rule(rule_id="expire_uploads_7d", rule_filter=None,
                                       status="Enabled", expiration=Expiration(days=7))]),
            )
            self._client.set_bucket_lifecycle(
                self._failed,
                LifecycleConfig([Rule(rule_id="expire_failed_30d", rule_filter=None,
                                       status="Enabled", expiration=Expiration(days=30))]),
            )
        except (S3Error, MaxRetryError, OSError) as e:
            raise StorageTransientError(f"configure_lifecycles failed: {e}") from e

    # ---- api + worker startup ----
    def assert_buckets_exist(self) -> None:
        """Read-only check. Raises StorageTransientError on missing bucket → fail-fast."""
        try:
            for bucket in (self._uploads, self._failed):
                if not self._client.bucket_exists(bucket):
                    raise StorageTransientError(f"bucket missing: {bucket}")
        except StorageTransientError:
            raise
        except (S3Error, MaxRetryError, OSError) as e:
            raise StorageTransientError(f"assert_buckets_exist failed: {e}") from e

    # ---- per-job ops ----
    def upload_file(self, key: str, data: bytes) -> None:
        try:
            self._client.put_object(
                self._uploads,
                key,
                io.BytesIO(data),
                length=len(data),
                content_type="application/octet-stream",
            )
        except (S3Error, MaxRetryError, OSError) as e:
            raise StorageTransientError(f"upload_file failed: {e}") from e

    def download_file(self, key: str) -> bytes:
        try:
            resp = self._client.get_object(self._uploads, key)
            try:
                return resp.read()
            finally:
                resp.close()
                resp.release_conn()
        except S3Error as e:
            if e.code in {"NoSuchKey", "NoSuchObject"}:
                raise ObjectNotFoundError(f"missing key={key}") from e
            raise StorageTransientError(f"download_file failed: {e}") from e
        except (MaxRetryError, OSError) as e:
            raise StorageTransientError(f"download_file failed: {e}") from e

    def delete_file(self, key: str) -> None:
        try:
            self._client.remove_object(self._uploads, key)
        except S3Error as e:
            if e.code in {"NoSuchKey", "NoSuchObject"}:
                logger.info("delete_skipped_missing", extra={"key": key})
                return
            raise StorageTransientError(f"delete_file failed: {e}") from e
        except (MaxRetryError, OSError) as e:
            raise StorageTransientError(f"delete_file failed: {e}") from e

    def move_to_failed(self, key: str) -> str:
        """Copy to failed-invoices/, then delete source. Returns new key.
        Idempotency at call site is enforced via failed_minio_key IS NULL guard."""
        new_key = f"{datetime.now(timezone.utc).strftime('%Y%m%d')}/{key}"
        try:
            self._client.copy_object(
                self._failed,
                new_key,
                CopySource(self._uploads, key),
            )
            self._client.remove_object(self._uploads, key)
        except S3Error as e:
            if e.code in {"NoSuchKey", "NoSuchObject"}:
                raise ObjectNotFoundError(f"move_to_failed missing key={key}") from e
            raise StorageTransientError(f"move_to_failed failed: {e}") from e
        except (MaxRetryError, OSError) as e:
            raise StorageTransientError(f"move_to_failed failed: {e}") from e
        return new_key

    def head_bucket(self) -> bool:
        """Probe BOTH buckets for /readyz. Returns True iff both exist.
        Connection error → False (do not raise; /readyz aggregates)."""
        try:
            return all(
                self._client.bucket_exists(b)
                for b in (self._uploads, self._failed)
            )
        except Exception:  # noqa: BLE001
            return False


# Singleton instantiated lazily — keeps imports cheap (matters for tests).
_singleton: MinIOClient | None = None


def get_minio() -> MinIOClient:
    global _singleton
    if _singleton is None:
        _singleton = MinIOClient()
    return _singleton


# Module-level handle most callers use directly.
class _LazyMinIO:
    def __getattr__(self, item):  # noqa: ANN001
        return getattr(get_minio(), item)


minio = _LazyMinIO()
