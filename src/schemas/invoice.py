"""Schemas — all OCR output fields are strings. Missing -> "" (never None)."""
from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

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


class SubmitRequest(BaseModel):
    """POST /v1/receipts JSON body."""

    model_config = ConfigDict(extra="forbid")

    image_url: HttpUrl


class SubmitResponse(BaseModel):
    """202 Accepted response from POST /v1/receipts."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    status: Literal["PENDING"] = "PENDING"
    message: str


class JobRecord(BaseModel):
    """Row projection from the jobs table."""

    model_config = ConfigDict(extra="forbid")

    job_id: UUID
    status: JobStatus
    phash: str | None = None
    image_url: str
    result: dict | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime

