"""Structured JSON logging. Called once at process start.

Every log record carries service, worker_id, and the current job_id contextvar
(when set) via the ContextFilter. stdout only — container runtime captures.
"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar

from src.config import settings

# Set by execute_task_lifecycle per job. Cleared on exit.
job_id_var: ContextVar[str | None] = ContextVar("job_id", default=None)


class ContextFilter(logging.Filter):
    def __init__(self, service: str):
        super().__init__()
        self._service = service
        self._worker_id = settings.WORKER_ID

    def filter(self, record: logging.LogRecord) -> bool:
        record.service = self._service
        record.worker_id = self._worker_id
        record.job_id = job_id_var.get()
        return True


def configure_logging(service: str) -> None:
    """Install the JSON formatter + ContextFilter on the root logger."""
    try:
        from pythonjsonlogger import jsonlogger  # type: ignore
    except ImportError:  # fall back to plain text if not installed
        jsonlogger = None  # type: ignore[assignment]

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)
    if jsonlogger is not None:
        fmt = jsonlogger.JsonFormatter(
            "%(asctime)s %(levelname)s %(service)s %(worker_id)s %(job_id)s %(name)s %(message)s",
            rename_fields={"asctime": "ts", "levelname": "level"},
            timestamp=True,
        )
    else:  # pragma: no cover
        fmt = logging.Formatter(
            "%(asctime)s %(levelname)s %(service)s %(worker_id)s job_id=%(job_id)s %(name)s: %(message)s"
        )
    handler.setFormatter(fmt)
    handler.addFilter(ContextFilter(service))
    root.addHandler(handler)
    root.setLevel(getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO))
