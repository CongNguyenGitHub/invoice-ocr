"""Domain-level enums and Redis key format constants."""
from __future__ import annotations

from enum import Enum


class JobStatus(str, Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    SUCCEEDED = "SUCCEEDED"
    FAILED_PERMANENT = "FAILED_PERMANENT"
    FAILED_TRANSIENT = "FAILED_TRANSIENT"


# Operator-controlled live rate limit hash.
REDIS_RATE_LIMIT_HASH = "ocr:rate_limit_config"

# Per-job bounded requeue counter.
REDIS_REQUEUE_HASH_FMT = "ocr:requeue:{job_id}"
REDIS_REQUEUE_FIELD = "count"
