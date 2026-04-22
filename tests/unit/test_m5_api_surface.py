"""M5 — light surface tests. Full lifecycle verified in integration (M5 gate)."""
from __future__ import annotations

import uuid

import pytest


def test_render_payload_success_returns_bare_invoice() -> None:
    from src.api.routes import _render_payload

    jid = uuid.uuid4()
    resp = _render_payload(
        jid,
        {"job_id": str(jid), "status": "SUCCEEDED", "result": {"name": "AEON"}},
    )
    assert resp.status_code == 200
    assert resp.body.decode().startswith('{"name":"AEON"')


def test_render_payload_permanent_returns_422() -> None:
    from src.api.routes import _render_payload

    jid = uuid.uuid4()
    resp = _render_payload(
        jid,
        {"job_id": str(jid), "status": "FAILED_PERMANENT", "error_code": "no_invoice_detected", "error_message": "x"},
    )
    assert resp.status_code == 422


def test_render_payload_transient_returns_503() -> None:
    from src.api.routes import _render_payload

    jid = uuid.uuid4()
    resp = _render_payload(
        jid,
        {"job_id": str(jid), "status": "FAILED_TRANSIENT", "error_code": "gemini_exhausted", "error_message": "x"},
    )
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_check_backpressure_rejects_at_threshold(monkeypatch) -> None:
    from fastapi import HTTPException

    from src.api import backpressure
    from src.config import settings

    async def fake_depth() -> int:
        return settings.BACKPRESSURE_QUEUE_REJECT + 1

    class FakeRedis:
        async def get_queue_depth(self):
            return await fake_depth()

    monkeypatch.setattr(backpressure, "redis", FakeRedis())

    with pytest.raises(HTTPException) as exc:
        await backpressure.check_backpressure()
    assert exc.value.status_code == 429
    assert exc.value.headers.get("Retry-After") == "5"


@pytest.mark.asyncio
async def test_check_backpressure_passes_under_warn(monkeypatch) -> None:
    from src.api import backpressure

    class FakeRedis:
        async def get_queue_depth(self):
            return 0

    monkeypatch.setattr(backpressure, "redis", FakeRedis())
    await backpressure.check_backpressure()  # no raise


def test_token_bucket_acquires_and_denies() -> None:
    import asyncio

    from src.utils.token_bucket import TokenBucket

    async def _run():
        b = TokenBucket(rate_per_second=1.0, burst=2)
        assert await b.acquire() is True
        assert await b.acquire() is True
        assert await b.acquire() is False

    asyncio.run(_run())
