"""M1 — storage-client import + surface tests.

These don't touch real Redis/PG/MinIO; they just verify the clients instantiate
and expose the spec'd methods. Integration tests that connect live backends
live under tests/integration/ and are gated on a docker-compose stack.
"""
from __future__ import annotations

import inspect


def test_minio_client_surface() -> None:
    from src.storage.minio_client import MinIOClient

    c = MinIOClient()
    for m in (
        "ensure_buckets_exist",
        "assert_buckets_exist",
        "configure_lifecycles",
        "upload_file",
        "download_file",
        "delete_file",
        "move_to_failed",
        "head_bucket",
    ):
        assert callable(getattr(c, m)), m


def test_postgres_client_surface() -> None:
    from src.storage.postgres_client import PostgresClient

    c = PostgresClient()
    for m in (
        "init_pool",
        "close_pool",
        "ping",
        "create_job_record",
        "update_job_status",
        "touch_updated_at",
        "get_job_record",
        "select_stale_jobs",
        "purge_old_job_records",
    ):
        fn = getattr(c, m)
        assert callable(fn), m
        assert inspect.iscoroutinefunction(fn), f"{m} must be async"


def test_redis_client_surface() -> None:
    from src.storage.redis_client import RedisClient

    c = RedisClient()
    for m in (
        "init",
        "close",
        "ping",
        "push_to_queue",
        "pop_from_queue",
        "get_queue_depth",
        "publish_result",
        "wait_for_result",
        "get_phash_cache",
        "set_phash_cache",
        "bump_requeue_counter",
        "read_rate_limit_config",
    ):
        fn = getattr(c, m)
        assert callable(fn), m
        assert inspect.iscoroutinefunction(fn), f"{m} must be async"


def test_alembic_migration_module_imports() -> None:
    # Ensure the initial migration file is syntactically valid + exposes upgrade/downgrade.
    import importlib.util
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[2]
        / "migrations"
        / "versions"
        / "0001_initial_jobs.py"
    )
    assert path.exists()
    spec = importlib.util.spec_from_file_location("mig_0001", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    assert callable(mod.upgrade)
    assert callable(mod.downgrade)
    assert mod.revision == "0001_initial_jobs"
    assert mod.down_revision is None
