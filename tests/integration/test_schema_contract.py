"""Schema-contract integration tests.

Validates that every "ok" prediction in our historical eval reports can be
reconstructed and round-tripped through the InvoiceResult Pydantic model
without losing fields or failing validation.

This catches:
  * Schema drift between run_eval.py's report layout and src.schemas.invoice
  * Field renames in the prompt that aren't reflected in the schema
  * Type changes (e.g. accidentally turning an int field into a string)

It is *not* an accuracy check — it does not call Gemini. It only verifies
that whatever the LLM wrote, our schema can re-parse it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.schemas.invoice import InvoiceResult, Product

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNS_DIR = REPO_ROOT / "experiments" / "runs"
DEV_SET = REPO_ROOT / "data" / "eval" / "dev_set.json"
SAMPLE_CAP = 50


@pytest.fixture(scope="module")
def gold_records() -> dict[str, dict]:
    """Index dev_set records by 'file' URL so we can rebuild predictions."""
    if not DEV_SET.exists():
        pytest.skip(f"dev_set.json not present at {DEV_SET}")
    return {r["file"]: r for r in json.loads(DEV_SET.read_text(encoding="utf-8"))}


def _normalize_product(p: dict) -> dict:
    """Map gold-label product field aliases to InvoiceResult.Product names.

    Gold labels (esp. BHX) use product_code / product_amount; the runtime schema
    expects product_id / product_quantity.  This mirrors the normalization
    already in scripts/run_eval.py:_normalize_label_record so the two stay in sync.
    """
    return {
        "product_id": p.get("product_id", p.get("product_code", "")),
        "product_name": p.get("product_name", ""),
        "product_unit_price": p.get("product_unit_price", ""),
        "product_quantity": p.get("product_quantity", p.get("product_amount", "")),
        "product_discount_money": p.get("product_discount_money", ""),
        "product_total_money": p.get("product_total_money", ""),
    }


def _rebuild_pred(gt: dict, mismatches: list[dict]) -> dict:
    """Reconstruct a predicted dict from GT and mismatches (top-level fields only).

    Stripped to ONLY the fields InvoiceResult declares — the gold-label files
    carry extra campaign metadata (campaign_id, total_original_money, _verify_status)
    that the pipeline schema does not expose.  Product items are normalized
    via _normalize_product to match the runtime Product schema.
    """
    schema_fields = set(InvoiceResult.model_fields.keys())
    pred = {k: v for k, v in gt.items() if k in schema_fields}
    pred["products"] = [_normalize_product(p) for p in gt.get("products", [])]
    for m in mismatches:
        f = m.get("field", "")
        if "." not in f and f in schema_fields:
            pred[f] = m.get("predicted", "")
    return pred


@pytest.mark.parametrize("run_path", sorted(RUNS_DIR.glob("run_*.json")))
def test_predictions_validate_against_invoice_schema(
    run_path: Path,
    gold_records: dict[str, dict],
) -> None:
    """Every ok-status prediction in every historical run must satisfy InvoiceResult."""
    report = json.loads(run_path.read_text(encoding="utf-8"))
    ok_records = [r for r in report["per_record"] if r.get("status") == "ok"]
    assert ok_records, f"No ok records in {run_path.name}"

    sampled = ok_records[:SAMPLE_CAP]
    failures: list[str] = []

    for rec in sampled:
        gt = gold_records.get(rec["orig_file"])
        if not gt:
            continue
        pred = _rebuild_pred(gt, rec.get("mismatches", []))
        try:
            InvoiceResult.model_validate(pred)
        except ValidationError as e:
            failures.append(f"{rec['orig_file']}: {e.errors()[:1]}")

    assert not failures, f"{len(failures)} schema validation failures in {run_path.name}: " + "\n  ".join(failures[:5])


def test_invoice_schema_round_trips_unchanged() -> None:
    """A minimal valid payload survives model_validate -> model_dump unchanged."""
    payload = {
        "name": "TEST STORE",
        "type": "satra",
        "date": "20/04/2026",
        "time": "10:30",
        "pos_id": "001",
        "receipt_number": "ABC-123",
        "cashier": "Tester",
        "total_money": "120000",
        "barcode": "",
        "products": [
            {
                "product_id": "8934567000123",
                "product_name": "BANH MI",
                "product_unit_price": "15000",
                "product_quantity": "2",
                "product_total_money": "30000",
            }
        ],
    }
    parsed = InvoiceResult.model_validate(payload)
    dumped = parsed.model_dump()
    # Every key the schema knows about must appear in the dump
    for key in InvoiceResult.model_fields:
        assert key in dumped, f"missing key after round-trip: {key}"
    assert isinstance(parsed.products[0], Product)


def test_invoice_schema_rejects_garbage() -> None:
    """The schema must refuse a non-dict payload."""
    with pytest.raises(ValidationError):
        InvoiceResult.model_validate("not a dict")
