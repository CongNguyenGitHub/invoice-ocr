"""Gemini extractor — native google-genai async SDK with strict response_schema.

Lazy initialization (decision #37): both SYSTEM_PROMPT and genai.Client load
on first call to extract_invoice, NOT at import time. This lets the API
container import this module without prompts/key (api never extracts).

Retry semantics:
  - ClientError(429)              → RateLimitedLocallyError (caller yields)
  - ServerError or asyncio.TimeoutError → backoff [0.3,0.6,1.2], then GeminiExhaustedError
  - ValidationError after schema enforcement → PermanentPipelineError("extractor_invalid_json")

Per-call timeout = settings.GEMINI_TIMEOUT_SECONDS (default 15 s).
"""
from __future__ import annotations

import asyncio
import io
import logging
import threading
from typing import Any

from PIL import Image
from pydantic import ValidationError

from src.config import settings
from src.domain.errors import (
    GeminiExhaustedError,
    PermanentPipelineError,
    RateLimitedLocallyError,
)
from src.pipeline.json_schema import LEGACY_JSON_SCHEMA
from src.schemas import InvoiceResult
from src.worker.metrics import (
    gemini_retries_total,
    gemini_tokens_total,
    llm_payload_bytes,
)

logger = logging.getLogger(__name__)

_client: Any | None = None
_system_prompt: str | None = None
_init_lock = threading.Lock()


def _load_system_prompt() -> str:
    global _system_prompt
    if _system_prompt is None:
        with _init_lock:
            if _system_prompt is None:
                path = settings.prompt_file_path()
                if not path.exists():
                    raise PermanentPipelineError(
                        "prompt_missing",
                        f"prompt file not found: {path} (PSV={settings.PROMPT_SEMANTIC_VERSION})",
                    )
                _system_prompt = path.read_text(encoding="utf-8")
    return _system_prompt


def _get_client() -> Any:
    global _client
    if _client is None:
        with _init_lock:
            if _client is None:
                if not settings.GEMINI_API_KEY:
                    raise PermanentPipelineError(
                        "gemini_api_key_missing", "GEMINI_API_KEY env var is empty"
                    )
                from google import genai

                _client = genai.Client(api_key=settings.GEMINI_API_KEY)
    return _client


def _encode_jpeg(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=settings.JPEG_QUALITY)
    return buf.getvalue()


async def extract_invoice(crop: Image.Image) -> InvoiceResult:
    """Run Gemini extraction. Caller has already acquired a token bucket slot."""
    from google import genai  # noqa: F401 — ensure SDK importable
    from google.genai import errors as genai_errors
    from google.genai import types as genai_types

    system_prompt = _load_system_prompt()
    client = _get_client()

    jpeg_bytes = _encode_jpeg(crop)
    llm_payload_bytes.observe(len(jpeg_bytes))

    config = genai_types.GenerateContentConfig(
        system_instruction=system_prompt,
        response_mime_type="application/json",
        response_schema=LEGACY_JSON_SCHEMA,
        temperature=0.0,
    )
    image_part = genai_types.Part.from_bytes(data=jpeg_bytes, mime_type="image/jpeg")

    last_exc: Exception | None = None
    backoffs = list(settings.GEMINI_BACKOFFS_SECONDS)
    for attempt in range(len(backoffs) + 1):
        try:
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=settings.GEMINI_MODEL,
                    contents=[image_part],
                    config=config,
                ),
                timeout=settings.GEMINI_TIMEOUT_SECONDS,
            )
            # Token usage
            try:
                usage = getattr(response, "usage_metadata", None)
                if usage is not None:
                    gemini_tokens_total.labels(kind="prompt").inc(
                        getattr(usage, "prompt_token_count", 0) or 0
                    )
                    gemini_tokens_total.labels(kind="candidates").inc(
                        getattr(usage, "candidates_token_count", 0) or 0
                    )
            except Exception:  # noqa: BLE001
                pass

            text = response.text or ""
            try:
                return InvoiceResult.model_validate_json(text)
            except ValidationError as ve:
                raise PermanentPipelineError(
                    "extractor_invalid_json", f"schema validation failed: {ve}"
                ) from ve

        except genai_errors.ClientError as e:
            status = getattr(e, "status_code", None) or getattr(e, "code", None)
            if status == 429:
                gemini_retries_total.labels(attempt="rate_limited").inc()
                raise RateLimitedLocallyError(
                    "gemini_rate_limited", "Gemini returned 429"
                ) from e
            # Other 4xx → permanent
            raise PermanentPipelineError(
                "gemini_client_error", f"{status}: {e}"
            ) from e

        except (genai_errors.ServerError, asyncio.TimeoutError) as e:
            last_exc = e
            gemini_retries_total.labels(attempt=str(attempt + 1)).inc()
            if attempt < len(backoffs):
                await asyncio.sleep(backoffs[attempt])
                continue
            raise GeminiExhaustedError(f"after {attempt + 1} attempts: {e}") from e

    raise GeminiExhaustedError(f"unreachable; last={last_exc}")
