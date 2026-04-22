"""Schemas — all OCR output fields are strings. Missing -> "" (never None)."""
from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from src.domain.constants import JobStatus


class Product(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: str = ""
    product_name: str = ""
    product_unit_price: str = ""
    product_quantity: str = ""
    product_discount_money: str = ""
    product_total_money: str = ""


class InvoiceResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = ""
    type: str = ""
    date: str = ""
    time: str = ""
    pos_id: str = ""
    receipt_number: str = ""
    cashier: str = ""
    total_money: str = ""
    barcode: str = ""
    products: list[Product] = Field(default_factory=list)


class JobRecord(BaseModel):
    """Row projection from the jobs table."""

    model_config = ConfigDict(extra="forbid")

    job_id: UUID
    status: JobStatus
    phash: str | None = None
    minio_key: str
    failed_minio_key: str | None = None
    result: dict | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime


class ErrorPayload(BaseModel):
    """Published to Redis + returned to poll clients on terminal failure."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    status: Literal["FAILED_PERMANENT", "FAILED_TRANSIENT"]
    error_code: str
    error_message: str


class SuccessPayload(BaseModel):
    """Published to Redis on SUCCESS — the `result` field is the canonical
    InvoiceResult JSON (as a dict)."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    status: Literal["SUCCEEDED"] = "SUCCEEDED"
    result: dict


class PendingEnvelope(BaseModel):
    """Returned on 504 (API waiting exceeded 60 s) or on GET-while-in-flight."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    status: Literal["PENDING", "PROCESSING"]
    message: str
