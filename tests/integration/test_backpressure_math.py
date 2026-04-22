"""Backpressure & token-bucket math integration tests.

Asserts the LLEN-threshold logic and token-bucket arithmetic without spinning
up Redis.  The unit suite already mocks individual components; this suite
exercises the *math* the production code relies on.

Catches regressions in:
  * BACKPRESSURE_QUEUE_REJECT / WARN being silently changed
  * TokenBucket refill formula (rate × elapsed_seconds)
  * TokenBucket burst clamping (max tokens never exceeds capacity)
  * Lock-free acquire returning False instead of blocking
"""
from __future__ import annotations

import asyncio
import time

import pytest

from src.config import settings
from src.utils.token_bucket import TokenBucket

# ──────────────────────────── Backpressure thresholds ───────────────────────


def test_backpressure_thresholds_are_sane() -> None:
    """REJECT must be > WARN must be > 0; both must be ints."""
    warn   = settings.BACKPRESSURE_QUEUE_WARN
    reject = settings.BACKPRESSURE_QUEUE_REJECT
    assert isinstance(warn,   int) and warn   > 0
    assert isinstance(reject, int) and reject > warn, (
        f"REJECT ({reject}) must be > WARN ({warn})"
    )


@pytest.mark.parametrize(
    "depth, want_429, want_warn",
    [
        (0,                                 False, False),
        (199,                               False, False),
        (200,                               False, True),    # exactly at WARN
        (settings.BACKPRESSURE_QUEUE_REJECT - 1, False, True),
        (settings.BACKPRESSURE_QUEUE_REJECT,     True,  True),  # exactly at REJECT
        (settings.BACKPRESSURE_QUEUE_REJECT + 1, True,  True),
    ],
)
def test_backpressure_decision_matrix(depth: int, want_429: bool, want_warn: bool) -> None:
    """The thresholds in settings.py must match the decision the API code makes:
       depth >= REJECT  -> 429
       depth >= WARN    -> warn (no 429)
    """
    is_reject = depth >= settings.BACKPRESSURE_QUEUE_REJECT
    is_warn   = depth >= settings.BACKPRESSURE_QUEUE_WARN
    assert is_reject == want_429,  f"REJECT decision wrong at depth={depth}"
    assert is_warn   == want_warn, f"WARN decision wrong at depth={depth}"


# ──────────────────────────── Token bucket math ─────────────────────────────


@pytest.mark.asyncio
async def test_token_bucket_drains_full_burst_immediately() -> None:
    bucket = TokenBucket(rate_per_second=4.0, burst=8)
    granted = [await bucket.acquire() for _ in range(8)]
    assert granted == [True] * 8, "bucket should grant exactly its burst"


@pytest.mark.asyncio
async def test_token_bucket_rejects_after_burst_exhausted() -> None:
    bucket = TokenBucket(rate_per_second=4.0, burst=8)
    for _ in range(8):
        assert await bucket.acquire()
    # 9th acquire within the same instant must be rejected
    assert not await bucket.acquire()


@pytest.mark.asyncio
async def test_token_bucket_refills_at_configured_rate() -> None:
    """At 10 tokens/s we should see at least 1 new token after 150 ms."""
    bucket = TokenBucket(rate_per_second=10.0, burst=2)
    for _ in range(2):
        assert await bucket.acquire()
    assert not await bucket.acquire()
    await asyncio.sleep(0.15)
    assert await bucket.acquire(), "expected at least 1 new token after 150ms"


@pytest.mark.asyncio
async def test_token_bucket_does_not_overflow_capacity() -> None:
    """After a long idle, available tokens must clamp to burst, not exceed it."""
    bucket = TokenBucket(rate_per_second=100.0, burst=4)
    await asyncio.sleep(0.1)              # 10 tokens of refill if uncapped
    granted = sum([await bucket.acquire() for _ in range(8)])
    assert granted == 4, f"expected exactly burst=4 grants, got {granted}"


@pytest.mark.asyncio
async def test_token_bucket_update_config_clamps_existing_tokens() -> None:
    bucket = TokenBucket(rate_per_second=4.0, burst=8)
    assert bucket.available == pytest.approx(8.0, abs=0.01)
    await bucket.update_config(rate_per_second=2.0, burst=4)
    assert bucket.available <= 4.0, "available must be clamped to new burst"


@pytest.mark.asyncio
async def test_token_bucket_acquire_is_nonblocking() -> None:
    """acquire() must return promptly even when empty — no >50ms hang."""
    bucket = TokenBucket(rate_per_second=0.001, burst=0)  # never grants
    t0 = time.monotonic()
    result = await bucket.acquire()
    elapsed_ms = (time.monotonic() - t0) * 1000
    assert result is False
    assert elapsed_ms < 50, f"acquire blocked for {elapsed_ms:.0f} ms"


# ──────────────────────────── Production config sanity ──────────────────────


def test_production_token_bucket_config_is_within_reason() -> None:
    """The default RPS/burst must be small enough not to swamp Gemini.
    If someone bumps these to 1000 RPS, this test fires.
    """
    assert 0.5 <= settings.TOKEN_BUCKET_RPS <= 50.0, (
        f"TOKEN_BUCKET_RPS={settings.TOKEN_BUCKET_RPS} outside sanity range"
    )
    assert 1 <= settings.TOKEN_BUCKET_BURST <= 100, (
        f"TOKEN_BUCKET_BURST={settings.TOKEN_BUCKET_BURST} outside sanity range"
    )
