"""Postprocessor — mandatory on every job (cache hits included).

Whitelist index is passed in (no module-global) — keeps API import surface
clean and makes unit tests trivial.

Vietnamese normalizer logic ported verbatim from the legacy pipeline (the
regexes are field-tested against real Aeon/BigC/Coopmart receipts).
"""

from __future__ import annotations

import logging
import re
import unicodedata

from src.schemas import InvoiceResult, Product

logger = logging.getLogger(__name__)


def _normalize_unicode(text: str) -> str:
    if not text:
        return ""
    return unicodedata.normalize("NFC", text).strip()


def _normalize_money(value: str) -> str:
    """Vietnamese money — dot/comma both used as thousand separator, never decimal."""
    if not value:
        return ""
    value = value.strip()
    value = re.sub(r"[đĐ₫\s]", "", value)
    is_negative = value.startswith("-") or value.startswith("(")
    value = value.strip("-()").strip()
    if not value:
        return ""
    value = value.replace(",", "").replace(".", "")
    value = re.sub(r"[^\d]", "", value)
    if not value:
        return ""
    return f"-{value}" if is_negative else value


def _normalize_date(date_str: str) -> str:
    """Fan-in DD/MM/YYYY · DD-MM-YYYY · DD.MM.YYYY · YYYY-MM-DD → DD/MM/YYYY."""
    if not date_str:
        return ""
    s = date_str.strip()
    if re.match(r"^\d{2}/\d{2}/\d{4}$", s):
        return s
    m = re.match(r"^(\d{2})-(\d{2})-(\d{4})$", s)
    if m:
        return f"{m.group(1)}/{m.group(2)}/{m.group(3)}"
    m = re.match(r"^(\d{2})\.(\d{2})\.(\d{4})$", s)
    if m:
        return f"{m.group(1)}/{m.group(2)}/{m.group(3)}"
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", s)
    if m:
        return f"{m.group(3)}/{m.group(2)}/{m.group(1)}"
    return s


def _normalize_time(time_str: str) -> str:
    if not time_str:
        return ""
    return time_str.strip()


def _normalize_quantity(qty_str: str) -> str:
    """Collapse '10.000' → '10' for integer quantities; preserve '0.47' for weighed."""
    if not qty_str:
        return ""
    s = qty_str.strip()
    try:
        num = float(s.replace(",", "."))
    except ValueError:
        return re.sub(r"[^\d.]", "", s)
    if num == int(num):
        return str(int(num))
    return str(num)


def _normalize_product(p: Product, index) -> Product:  # noqa: ANN001
    return Product(
        product_id=_normalize_unicode(p.product_id),
        product_name=index.match_product(p.product_name),
        product_unit_price=_normalize_money(p.product_unit_price),
        product_quantity=_normalize_quantity(p.product_quantity),
        product_discount_money=_normalize_money(p.product_discount_money),
        product_total_money=_normalize_money(p.product_total_money),
    )


def postprocess(result: InvoiceResult, index) -> InvoiceResult:  # noqa: ANN001
    """Order matches arch §5.5."""
    return InvoiceResult(
        name=index.match_store(result.name),
        type=(result.type.strip().lower() if result.type else ""),
        date=_normalize_date(result.date),
        time=_normalize_time(result.time),
        pos_id=_normalize_unicode(result.pos_id),
        receipt_number=_normalize_unicode(result.receipt_number),
        cashier=_normalize_unicode(result.cashier),
        total_money=_normalize_money(result.total_money),
        barcode=_normalize_unicode(result.barcode),
        products=[_normalize_product(p, index) for p in result.products],
    )
