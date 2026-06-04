"""Central configuration — pydantic-settings singleton.

Single source of truth for every environment variable. No other module may read
os.environ directly. Helper methods on this class are the only place where
Redis/prompt key formats are computed.
"""
from __future__ import annotations

from pathlib import Path
from uuid import UUID

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # ---------- API ----------
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000
    API_MAX_IMAGE_BYTES: int = 10_485_760  # 10 MB
    API_METRICS_PORT: int = 9101

    # ---------- Image Download (Worker → CDN) ----------
    IMAGE_DOWNLOAD_TIMEOUT_SECONDS: int = 30
    IMAGE_DOWNLOAD_MAX_BYTES: int = 10_485_760  # 10 MB
    ALLOWED_IMAGE_DOMAINS: list[str] = Field(default_factory=lambda: ["img-campaign.gotit.vn"])

    # ---------- Worker ----------
    WORKER_METRICS_PORT: int = 9102
    WORKER_CONCURRENCY: int = 4

    # ---------- Redis ----------
    REDIS_URL: str = "redis://redis:6379"
    REDIS_QUEUE_KEY: str = "ocr:queue"
    REDIS_PHASH_TTL_SECONDS: int = 86_400
    REDIS_REQUEUE_TTL_SECONDS: int = 3_600

    # ---------- Postgres ----------
    POSTGRES_DSN: str = "postgresql+asyncpg://invoice:invoice@postgres:5432/invoice_ocr"

    # ---------- Triton / YOLO ----------
    TRITON_HOST: str = "triton:8001"
    YOLO_MODEL_NAME: str = "yolov11n_receipt"
    YOLO_CONFIDENCE_THRESHOLD: float = 0.35
    YOLO_CROP_PAD_PERCENT: float = 0.02

    # ---------- Preprocess ----------
    MAX_IMAGE_DIMENSION: int = 1_600
    JPEG_QUALITY: int = 85

    # ---------- Gemini ----------
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-3.1-flash-lite-preview"
    GEMINI_TIMEOUT_SECONDS: int = 15
    GEMINI_BACKOFFS_SECONDS: list[float] = Field(default_factory=lambda: [0.3, 0.6, 1.2])

    # ---------- Prompt ----------
    PROMPT_SEMANTIC_VERSION: str = "v3.7"

    # ---------- Rate limiting ----------
    TOKEN_BUCKET_RPS: float = 4.0
    TOKEN_BUCKET_BURST: int = 8
    RATE_LIMIT_REFRESH_INTERVAL: int = 30

    # ---------- Whitelists ----------
    WHITELIST_DIR: str = "/app/whitelists"

    # ---------- Sweeper ----------
    SWEEP_INTERVAL_SECONDS: int = 60
    STALE_PROCESSING_MINUTES: int = 15
    STALE_PENDING_MINUTES: int = 30
    REQUEUE_MAX: int = 3

    # ---------- Backpressure ----------
    BACKPRESSURE_QUEUE_WARN: int = 200
    BACKPRESSURE_QUEUE_REJECT: int = 500

    # ---------- Worker identity ----------
    WORKER_ID: str = "worker-1"
    PURGE_WORKER_ID: str = "worker-1"
    JOB_RETENTION_DAYS: int = 90

    # ---------- Logging ----------
    LOG_LEVEL: str = "INFO"

    def phash_cache_key(self, phash: str) -> str:
        return f"ocr:phash:{phash}:psv:{self.PROMPT_SEMANTIC_VERSION}"

    def requeue_key(self, job_id: UUID | str) -> str:
        return f"ocr:requeue:{job_id}"

    def prompt_file_path(self) -> Path:
        # src/config/settings.py → parent.parent == src/
        # prompts live at src/pipeline/prompts/{PSV}.txt
        return (
            Path(__file__).parent.parent
            / "pipeline"
            / "prompts"
            / f"{self.PROMPT_SEMANTIC_VERSION}.txt"
        )


settings = Settings()
