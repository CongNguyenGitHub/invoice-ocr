"""Exception hierarchy — see task §12.

Every concrete class carries:
  * error_code  — stable string, surfaced in Postgres + API error responses.
  * message     — human-readable.
  * is_permanent (class attribute) — routes to FAILED_PERMANENT vs FAILED_TRANSIENT.
"""

from __future__ import annotations


class OCRSystemError(Exception):
    """Base for every domain error. Never raised directly."""

    error_code: str = "ocr_system_error"
    is_permanent: bool = False

    def __init__(self, error_code: str | None = None, message: str = ""):
        self.error_code = error_code or self.error_code
        self.message = message or self.error_code
        super().__init__(self.message)

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.error_code}: {self.message}"


# -------------------- Ingress --------------------
class IngressError(OCRSystemError):
    is_permanent = True


class UnsupportedMediaType(IngressError):  # noqa: N818 — domain error name, not *Error
    error_code = "unsupported_media_type"
    is_permanent = True

    def __init__(self, message: str = "Content-Type not in image/{jpeg,png,webp}"):
        super().__init__(self.error_code, message)


class PayloadTooLarge(IngressError):  # noqa: N818 — domain error name, not *Error
    error_code = "payload_too_large"
    is_permanent = True

    def __init__(self, message: str = "Upload exceeds API_MAX_IMAGE_BYTES"):
        super().__init__(self.error_code, message)


# -------------------- Pipeline --------------------
class PipelineError(OCRSystemError):
    pass


class PermanentPipelineError(PipelineError):
    """Terminal — no retry will help (truncated upload, no YOLO detection,
    invalid JSON after strict-schema extraction)."""

    is_permanent = True

    def __init__(self, error_code: str, message: str = ""):
        super().__init__(error_code, message)


class TransientPipelineError(PipelineError):
    """Parent type for retryables. Rarely raised directly — serves as catch-tuple
    member so subclass additions don't require touching every call site."""

    is_permanent = False


class RateLimitedLocallyError(PipelineError):
    """Token bucket empty OR Gemini returned 429 — yield slot back to queue."""

    is_permanent = False

    def __init__(self, error_code: str = "rate_limited_locally", message: str = ""):
        super().__init__(error_code, message)


class GeminiExhaustedError(TransientPipelineError):
    """All 3 backoff attempts exhausted."""

    error_code = "gemini_exhausted"

    def __init__(self, message: str = "Gemini retries exhausted"):
        super().__init__(self.error_code, message)


class TritonUnavailableError(TransientPipelineError):
    """gRPC to Triton refused / unreachable."""

    error_code = "triton_unavailable"

    def __init__(self, message: str = "Triton inference server unavailable"):
        super().__init__(self.error_code, message)


# -------------------- Storage --------------------
class StorageTransientError(OCRSystemError):
    """Network / 5xx from any storage backend."""

    error_code = "storage_transient"
    is_permanent = False

    def __init__(self, message: str = "Storage backend unavailable"):
        super().__init__(self.error_code, message)


class DatabaseUnavailable(OCRSystemError):  # noqa: N818 — domain error name, not *Error
    """asyncpg PostgresError — mapped via @_wrap_pg_errors decorator."""

    error_code = "database_unavailable"
    is_permanent = False

    def __init__(self, message: str = "Postgres unavailable"):
        super().__init__(self.error_code, message)
