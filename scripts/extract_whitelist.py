"""
extract_whitelist.py -- Rebuild store/product whitelists from verified gold labels.

Reads label_verified.json (or any label file), filters to records whose
_verify_status is in the allowed set, then writes two JSON arrays:
  • whitelists/store_names_whitelist.json
  • whitelists/product_names_whitelist.json

Unicode NFC-normalises every entry before deduplication so that visually
identical strings from different OCR runs collapse to one canonical form.

Usage
─────
    # Standard rebuild from verified gold (default)
    python scripts/extract_whitelist.py

    # Explicit paths / status filter
    python scripts/extract_whitelist.py \\
        --input label_verified.json \\
        --store-out  whitelists/store_names_whitelist.json \\
        --product-out whitelists/product_names_whitelist.json \\
        --status-filter verified_ok corrected
"""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s.strip())


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", default="label_verified.json", help="Source label file (default: label_verified.json)")
    ap.add_argument("--store-out", default="whitelists/store_names_whitelist.json")
    ap.add_argument("--product-out", default="whitelists/product_names_whitelist.json")
    ap.add_argument(
        "--status-filter",
        nargs="+",
        default=["verified_ok", "corrected"],
        help="Only include records with these _verify_status values",
    )
    ap.add_argument("--include-all", action="store_true", help="Ignore _verify_status filter (include every record)")
    args = ap.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

    src = Path(args.input)
    if not src.exists():
        print(f"ERROR: input file not found: {src}", file=sys.stderr)
        return 1

    records: list[dict] = json.loads(src.read_text(encoding="utf-8"))
    allowed_statuses = set(args.status_filter)

    # Filter
    if args.include_all:
        usable = records
    else:
        usable = [r for r in records if r.get("_verify_status") in allowed_statuses]

    print(f"Total records  : {len(records)}")
    print(f"Status filter  : {sorted(allowed_statuses)}")
    print(f"Usable records : {len(usable)}")
    print()

    # Collect -- skip records with missing type/name
    store_names: set[str] = set()
    product_names: set[str] = set()
    by_type: dict[str, int] = defaultdict(int)
    prod_by_type: dict[str, int] = defaultdict(int)

    for rec in usable:
        t = rec.get("type", "unknown")
        name = rec.get("name", "")
        if name:
            store_names.add(nfc(name))
            by_type[t] += 1

        for p in rec.get("products") or []:
            if not isinstance(p, dict):
                continue
            pname = p.get("product_name", "")
            if pname:
                product_names.add(nfc(pname))
                prod_by_type[t] += 1

    # Sort
    store_list = sorted(store_names)
    product_list = sorted(product_names)

    # Write
    for path_str, data in [
        (args.store_out, store_list),
        (args.product_out, product_list),
    ]:
        p = Path(path_str)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # Report
    print(f"Store names    : {len(store_list):5d}  -> {args.store_out}")
    print(f"Product names  : {len(product_list):5d}  -> {args.product_out}")
    print()
    print("Store name count by type:")
    for t, cnt in sorted(by_type.items()):
        print(f"  {t:15s}: {cnt} records")
    print()
    print("Product count by type:")
    for t, cnt in sorted(prod_by_type.items()):
        print(f"  {t:15s}: {cnt} products")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
