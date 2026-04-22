"""M2 — pipeline core unit tests (no network)."""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from PIL import Image


# -------------------- preprocessor --------------------
def _make_jpeg(size=(800, 1200), color=(200, 200, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def test_preprocess_returns_two_field_result_with_phash() -> None:
    from src.pipeline.preprocessor import preprocess_image

    pp = preprocess_image(_make_jpeg())
    assert pp.phash and len(pp.phash) >= 8
    assert pp.pil.mode == "RGB"


def test_preprocess_resizes_oversized() -> None:
    from src.config import settings
    from src.pipeline.preprocessor import preprocess_image

    pp = preprocess_image(_make_jpeg(size=(6000, 6000)))
    assert max(pp.pil.size) <= settings.MAX_IMAGE_DIMENSION


def test_preprocess_truncated_raises_permanent() -> None:
    from src.domain.errors import PermanentPipelineError
    from src.pipeline.preprocessor import preprocess_image

    raw = _make_jpeg()[:64]
    with pytest.raises(PermanentPipelineError):
        preprocess_image(raw)


# -------------------- postprocessor --------------------
def test_normalize_money_vietnamese() -> None:
    from src.pipeline.postprocessor import _normalize_money

    assert _normalize_money("1.356.000") == "1356000"
    assert _normalize_money("1,356,000") == "1356000"
    assert _normalize_money("452.000 đ") == "452000"
    assert _normalize_money("-24,000") == "-24000"
    assert _normalize_money("") == ""


def test_normalize_date_fans_in() -> None:
    from src.pipeline.postprocessor import _normalize_date

    assert _normalize_date("19/04/2026") == "19/04/2026"
    assert _normalize_date("19-04-2026") == "19/04/2026"
    assert _normalize_date("19.04.2026") == "19/04/2026"
    assert _normalize_date("2026-04-19") == "19/04/2026"


def test_normalize_quantity_collapses_integers() -> None:
    from src.pipeline.postprocessor import _normalize_quantity

    # Spec: float-with-integer-collapse — "10.000" parses to 10.0 → "10"
    assert _normalize_quantity("10.000") == "10"
    assert _normalize_quantity("10") == "10"
    assert _normalize_quantity("0.47") == "0.47"


# -------------------- whitelist index --------------------
def _wl_dir(tmp_path: Path, stores: list[str], products: list[str]) -> str:
    (tmp_path / "store_names_whitelist.json").write_text(json.dumps(stores), encoding="utf-8")
    (tmp_path / "product_names_whitelist.json").write_text(json.dumps(products), encoding="utf-8")
    return str(tmp_path)


def test_whitelist_match_store_exact_and_fuzzy(tmp_path: Path) -> None:
    from src.pipeline.whitelist_index import WhitelistIndex

    wl_dir = _wl_dir(tmp_path, ["AEON Mall Tân Phú", "Co.opmart Đinh Tiên Hoàng"], [])
    idx = WhitelistIndex.build(wl_dir)

    assert idx.match_store("aeon mall tân phú") == "AEON Mall Tân Phú"
    # Slight typo — should fuzzy-match above 80
    assert idx.match_store("AEON Mall Tan Phu") == "AEON Mall Tân Phú"
    # Way off — falls below cutoff, returns NFC raw
    assert idx.match_store("Saigon Co-op Mart Quận 5") != ""


def test_whitelist_product_no_fallback_returns_nfc_raw(tmp_path: Path) -> None:
    from src.pipeline.whitelist_index import WhitelistIndex

    wl_dir = _wl_dir(tmp_path, [], ["Sữa tươi Vinamilk 1L"])
    idx = WhitelistIndex.build(wl_dir)
    # Completely unrelated product — should NOT scan-fallback (kind="product")
    assert idx.match_product("xyzabcunknown") == "xyzabcunknown"


def test_whitelist_reload_swaps_atomically(tmp_path: Path) -> None:
    from src.pipeline.whitelist_index import WhitelistIndex

    wl_dir = _wl_dir(tmp_path, ["Old Store"], [])
    idx = WhitelistIndex.build(wl_dir)
    assert idx.match_store("old store") == "Old Store"

    (tmp_path / "store_names_whitelist.json").write_text(
        json.dumps(["New Store"]), encoding="utf-8"
    )
    idx.reload("store", tmp_path / "store_names_whitelist.json")
    assert idx.match_store("new store") == "New Store"


# -------------------- json_schema --------------------
def test_legacy_json_schema_strict() -> None:
    """Gemini's response_schema subset rejects `additionalProperties`.
    Drift-rejection is enforced client-side by Pydantic `extra="forbid"`
    on InvoiceResult/Product. Schema must be free of that key everywhere.
    """
    from src.pipeline.json_schema import LEGACY_JSON_SCHEMA

    assert "additionalProperties" not in LEGACY_JSON_SCHEMA
    assert "products" in LEGACY_JSON_SCHEMA["required"]
    assert "additionalProperties" not in LEGACY_JSON_SCHEMA["properties"]["products"]["items"]
