"""
split_eval_set.py -- Stratified 70/30 dev/test split of the verified gold label set.

Source  : label_verified.json  (1,443 records, each with _verify_status)
Usable  : _verify_status in {verified_ok, corrected}  (~1,356 records)
Excluded: UNCORRECTABLE_IDX = {161, 589, 699, 765, 778, 1183}
          + records missing date/receipt_number/total_money

Split rule
──────────
  70% dev  / 30% test, **stratified by `type`** (8 store types)
  Deterministic: random.seed(42)
  coopxtra floor: always at least 2 in test (6 total -> 4 dev / 2 test)

Outputs
───────
  data/eval/dev_set.json        ~950 records
  data/eval/test_set.json       ~406 records
  data/eval/split_meta.csv      golden_index, type, split, file_url, verify_status

Usage
─────
    python scripts/split_eval_set.py
    python scripts/split_eval_set.py --input label_verified.json --out-dir data/eval
    python scripts/split_eval_set.py --test-ratio 0.30 --seed 42
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sys
from collections import defaultdict
from pathlib import Path

UNCORRECTABLE_IDX: set[int] = {161, 589, 699, 765, 778, 1183}
USABLE_STATUSES = {"verified_ok", "corrected"}


def _is_missing_critical(inv: dict) -> bool:
    for f in ("date", "receipt_number", "total_money"):
        if not str(inv.get(f) or "").strip():
            return True
    return False


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", default="label_verified.json")
    ap.add_argument("--out-dir", default="data/eval")
    ap.add_argument("--test-ratio", type=float, default=0.30, help="Fraction for test set (default 0.30 -> 30%%)")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

    src = Path(args.input)
    if not src.exists():
        print(f"ERROR: {src} not found", file=sys.stderr)
        return 1

    all_records: list[dict] = json.loads(src.read_text(encoding="utf-8"))

    # Build usable set with original indices
    usable: list[tuple[int, dict]] = []
    for idx, rec in enumerate(all_records):
        if idx in UNCORRECTABLE_IDX:
            continue
        if rec.get("_verify_status") not in USABLE_STATUSES:
            continue
        if _is_missing_critical(rec):
            continue
        usable.append((idx, rec))

    print(f"Total records  : {len(all_records)}")
    print(f"Usable records : {len(usable)}")

    # Group by type -- normalise to lowercase to collapse e.g. "Emart" -> "emart"
    by_type: dict[str, list[tuple[int, dict]]] = defaultdict(list)
    for orig_idx, rec in usable:
        store_type = (rec.get("type") or "unknown").lower()
        by_type[store_type].append((orig_idx, rec))

    rng = random.Random(args.seed)

    dev_items: list[tuple[int, dict]] = []
    test_items: list[tuple[int, dict]] = []

    print(f"\nStratified split  (test_ratio={args.test_ratio}, seed={args.seed}):")
    print(f"{'type':15s}  {'total':>6}  {'dev':>6}  {'test':>6}")
    print("-" * 42)

    for store_type, items in sorted(by_type.items()):
        rng.shuffle(items)
        n_total = len(items)
        if store_type == "coopxtra":
            # Hard floor: at least 2 in test
            n_test = max(2, math.floor(n_total * args.test_ratio))
        else:
            n_test = math.floor(n_total * args.test_ratio)
        n_dev = n_total - n_test

        dev_items.extend(items[:n_dev])
        test_items.extend(items[n_dev:])
        print(f"  {store_type:13s}  {n_total:6d}  {n_dev:6d}  {n_test:6d}")

    print("-" * 42)
    print(f"  {'TOTAL':13s}  {len(usable):6d}  {len(dev_items):6d}  {len(test_items):6d}")

    # Sanity check: no overlap
    dev_indices = {i for i, _ in dev_items}
    test_indices = {i for i, _ in test_items}
    assert dev_indices.isdisjoint(test_indices), "BUG: dev/test overlap!"

    # Shuffle final lists (so type order is mixed)
    rng.shuffle(dev_items)
    rng.shuffle(test_items)

    # Write JSON splits
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    def _records(items: list[tuple[int, dict]]) -> list[dict]:
        return [rec for _, rec in items]

    dev_path = out_dir / "dev_set.json"
    test_path = out_dir / "test_set.json"
    dev_path.write_text(json.dumps(_records(dev_items), ensure_ascii=False, indent=2), encoding="utf-8")
    test_path.write_text(json.dumps(_records(test_items), ensure_ascii=False, indent=2), encoding="utf-8")

    # Write CSV meta
    csv_path = out_dir / "split_meta.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "golden_index",
                "type",
                "split",
                "file_url",
                "verify_status",
            ],
        )
        writer.writeheader()
        for orig_idx, rec in dev_items:
            writer.writerow(
                {
                    "golden_index": orig_idx,
                    "type": rec.get("type", ""),
                    "split": "dev",
                    "file_url": rec.get("file", ""),
                    "verify_status": rec.get("_verify_status", ""),
                }
            )
        for orig_idx, rec in test_items:
            writer.writerow(
                {
                    "golden_index": orig_idx,
                    "type": rec.get("type", ""),
                    "split": "test",
                    "file_url": rec.get("file", ""),
                    "verify_status": rec.get("_verify_status", ""),
                }
            )

    print("\nOutputs:")
    print(f"  {dev_path}   ({len(dev_items)} records)")
    print(f"  {test_path}  ({len(test_items)} records)")
    print(f"  {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
