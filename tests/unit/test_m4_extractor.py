"""M4 — extractor unit tests (google-genai mocked)."""
from __future__ import annotations

import asyncio
import json
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image


# ----- shared fakes -----
class _FakeClientError(Exception):
    def __init__(self, status_code: int, message: str = "boom") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = status_code


class _FakeServerError(Exception):
    pass


def _install_fake_genai(monkeypatch, generate: AsyncMock) -> None:
    """Install a fake `google.genai` package that captures calls to generate_content."""
    google_pkg = types.ModuleType("google")
    genai_pkg = types.ModuleType("google.genai")
    errors_mod = types.ModuleType("google.genai.errors")
    types_mod = types.ModuleType("google.genai.types")

    errors_mod.ClientError = _FakeClientError
    errors_mod.ServerError = _FakeServerError

    class _Part:
        @classmethod
        def from_bytes(cls, data, mime_type):
            return ("part", mime_type, len(data))

    class _Cfg:
        def __init__(self, **kw):
            self.kw = kw

    types_mod.Part = _Part
    types_mod.GenerateContentConfig = _Cfg

    aio_models = MagicMock()
    aio_models.generate_content = generate
    fake_aio = MagicMock(models=aio_models)
    MagicMock(aio=fake_aio)

    class _GenaiClient:
        def __init__(self, api_key: str) -> None:
            self.api_key = api_key
            self.aio = fake_aio

    genai_pkg.Client = _GenaiClient
    genai_pkg.errors = errors_mod
    genai_pkg.types = types_mod
    google_pkg.genai = genai_pkg

    monkeypatch.setitem(sys.modules, "google", google_pkg)
    monkeypatch.setitem(sys.modules, "google.genai", genai_pkg)
    monkeypatch.setitem(sys.modules, "google.genai.errors", errors_mod)
    monkeypatch.setitem(sys.modules, "google.genai.types", types_mod)


def _reset_extractor_singletons() -> None:
    from src.pipeline import extractor

    extractor._client = None
    extractor._system_prompt = None


def _img() -> Image.Image:
    return Image.new("RGB", (100, 100), (255, 255, 255))


def _valid_invoice_json() -> str:
    return json.dumps(
        {
            "name": "AEON",
            "type": "supermarket",
            "date": "19/04/2026",
            "time": "10:30",
            "pos_id": "P01",
            "receipt_number": "R123",
            "cashier": "C1",
            "total_money": "100000",
            "barcode": "",
            "products": [],
        }
    )


@pytest.mark.asyncio
async def test_extractor_returns_validated_invoice(monkeypatch) -> None:
    from src.config import settings

    _reset_extractor_singletons()
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "fake-key", raising=False)

    response = MagicMock(text=_valid_invoice_json(), usage_metadata=MagicMock(
        prompt_token_count=10, candidates_token_count=20
    ))
    gen = AsyncMock(return_value=response)
    _install_fake_genai(monkeypatch, gen)

    from src.pipeline.extractor import extract_invoice

    result = await extract_invoice(_img())
    assert result.name == "AEON"
    assert result.total_money == "100000"
    gen.assert_awaited_once()


@pytest.mark.asyncio
async def test_extractor_429_yields_rate_limited(monkeypatch) -> None:
    from src.config import settings
    from src.domain.errors import RateLimitedLocallyError

    _reset_extractor_singletons()
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "fake-key", raising=False)

    gen = AsyncMock(side_effect=_FakeClientError(429))
    _install_fake_genai(monkeypatch, gen)

    from src.pipeline.extractor import extract_invoice

    with pytest.raises(RateLimitedLocallyError):
        await extract_invoice(_img())
    assert gen.await_count == 1  # NO retry on 429


@pytest.mark.asyncio
async def test_extractor_5xx_retries_then_exhausts(monkeypatch) -> None:
    from src.config import settings
    from src.domain.errors import GeminiExhaustedError

    _reset_extractor_singletons()
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "fake-key", raising=False)
    monkeypatch.setattr(settings, "GEMINI_BACKOFFS_SECONDS", [0.0, 0.0, 0.0], raising=False)

    gen = AsyncMock(side_effect=_FakeServerError("500"))
    _install_fake_genai(monkeypatch, gen)

    # patch asyncio.sleep to no-op for speed (it's already 0 but defensive)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    from src.pipeline.extractor import extract_invoice

    try:
        with pytest.raises(GeminiExhaustedError):
            await extract_invoice(_img())
    finally:
        monkeypatch.setattr(asyncio, "sleep", real_sleep)
    assert gen.await_count == 4  # initial + 3 retries


@pytest.mark.asyncio
async def test_extractor_prompt_missing_raises_on_first_call(monkeypatch, tmp_path) -> None:
    """Critical: prompt missing should NOT raise at import — only on first call."""
    from src.config import settings
    from src.domain.errors import PermanentPipelineError

    _reset_extractor_singletons()
    monkeypatch.setattr(settings, "PROMPT_SEMANTIC_VERSION", "vXX_nope", raising=False)
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "fake-key", raising=False)

    gen = AsyncMock()
    _install_fake_genai(monkeypatch, gen)

    # Module already imported by other tests — this proves import did not raise.
    from src.pipeline.extractor import extract_invoice

    with pytest.raises(PermanentPipelineError) as exc:
        await extract_invoice(_img())
    assert exc.value.error_code == "prompt_missing"
    gen.assert_not_awaited()


@pytest.mark.asyncio
async def test_extractor_invalid_json_is_permanent(monkeypatch) -> None:
    from src.config import settings
    from src.domain.errors import PermanentPipelineError

    _reset_extractor_singletons()
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "fake-key", raising=False)

    response = MagicMock(text='{"bogus": true}', usage_metadata=None)
    gen = AsyncMock(return_value=response)
    _install_fake_genai(monkeypatch, gen)

    from src.pipeline.extractor import extract_invoice

    with pytest.raises(PermanentPipelineError) as exc:
        await extract_invoice(_img())
    assert exc.value.error_code == "extractor_invalid_json"
