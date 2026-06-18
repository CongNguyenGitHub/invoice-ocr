"""M0 smoke test — verifies the tree imports and settings helpers work.
No network, no Docker — runs under plain `pytest -q`.
"""

from __future__ import annotations

import pytest


def test_settings_import() -> None:
    from src.config import settings

    assert settings.API_PORT == 8000
    assert settings.WORKER_METRICS_PORT != settings.API_METRICS_PORT
    assert settings.REQUEUE_MAX == 3


def test_redis_key_helpers_contain_psv() -> None:
    from src.config import settings

    key = settings.phash_cache_key("deadbeef")
    assert "psv:" in key
    assert settings.PROMPT_SEMANTIC_VERSION in key


def test_prompt_file_path_points_into_pipeline_prompts() -> None:
    from src.config import settings

    p = settings.prompt_file_path()
    assert p.name == f"{settings.PROMPT_SEMANTIC_VERSION}.txt"
    assert p.parent.name == "prompts"
    assert p.parent.parent.name == "pipeline"


def test_schema_all_strings_missing_empty() -> None:
    from src.schemas import InvoiceResult

    r = InvoiceResult()
    assert r.name == ""
    assert r.products == []


def test_schema_extra_forbid() -> None:
    from pydantic import ValidationError

    from src.schemas import InvoiceResult

    with pytest.raises(ValidationError):
        InvoiceResult(extra_field="boom")  # type: ignore[call-arg]


def test_error_hierarchy_exposes_error_code() -> None:
    from src.domain.errors import (
        OCRSystemError,
        PermanentPipelineError,
        StorageTransientError,
    )

    assert StorageTransientError.is_permanent is False
    assert PermanentPipelineError("foo").error_code == "foo"
    assert issubclass(PermanentPipelineError, OCRSystemError)


def test_jobstatus_enum_values() -> None:
    from src.domain.constants import JobStatus

    assert JobStatus.SUCCEEDED.value == "SUCCEEDED"
    assert {s.value for s in JobStatus} == {
        "PENDING",
        "PROCESSING",
        "SUCCEEDED",
        "FAILED_PERMANENT",
        "FAILED_TRANSIENT",
    }
