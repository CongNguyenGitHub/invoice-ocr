"""LEGACY_JSON_SCHEMA — strict structured-output schema for Gemini.

Mirrors src/schemas/invoice.py exactly. Gemini's response_schema subset of
JSON-Schema does NOT accept `additionalProperties`; drift-rejection is
enforced client-side by Pydantic `extra="forbid"` on InvoiceResult/Product,
which surfaces drift as ValidationError → PermanentPipelineError(
"extractor_invalid_json").
"""
from __future__ import annotations

_PRODUCT_SCHEMA = {
    "type": "object",
    "required": [
        "product_id",
        "product_name",
        "product_unit_price",
        "product_quantity",
        "product_discount_money",
        "product_total_money",
    ],
    "properties": {
        "product_id": {"type": "string"},
        "product_name": {"type": "string"},
        "product_unit_price": {"type": "string"},
        "product_quantity": {"type": "string"},
        "product_discount_money": {"type": "string"},
        "product_total_money": {"type": "string"},
    },
}

LEGACY_JSON_SCHEMA = {
    "type": "object",
    "required": [
        "name",
        "type",
        "date",
        "time",
        "pos_id",
        "receipt_number",
        "cashier",
        "total_money",
        "barcode",
        "products",
    ],
    "properties": {
        "name": {"type": "string"},
        "type": {"type": "string"},
        "date": {"type": "string"},
        "time": {"type": "string"},
        "pos_id": {"type": "string"},
        "receipt_number": {"type": "string"},
        "cashier": {"type": "string"},
        "total_money": {"type": "string"},
        "barcode": {"type": "string"},
        "products": {"type": "array", "items": _PRODUCT_SCHEMA},
    },
}
