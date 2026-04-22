"""
sample_test_set.py — Create the A/B testing set from golden_clean.json.

Stratified sampling of 300 invoices.
Allocation (hard-floor on coopxtra=5):
  satra: 49, lotte: 44, coopmart: 43, bigc: 42, bhx_2024: 42, emart: 41, aeon: 34, coopxtra: 5

Output:
  data/ab_test/test_set_300.json
  data/ab_test/test_set_300_meta.csv
"""
import argparse
import csv
import json
import pathlib
import random

ALLOCATION = {
    "satra": 49,
    "lotte": 44,
    "coopmart": 43,
    "bigc": 42,
    "bhx_2024": 42,
    "emart": 41,
    "aeon": 34,
    "coopxtra": 5,
}
SEED = 42

def main(src: str, out_dir: str):
    out = pathlib.Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    with open(src, encoding="utf-8") as f:
        data = json.load(f)

    # Group by type
    by_type = {}
    for idx, rec in enumerate(data):
        t = rec.get("type", "UNKNOWN")
        by_type.setdefault(t, []).append((idx, rec))

    rng = random.Random(SEED)
    sampled = []
    meta_rows = []

    for type_name, quota in ALLOCATION.items():
        pool = by_type.get(type_name, [])
        if len(pool) < quota:
            raise ValueError(f"{type_name}: need {quota} but only {len(pool)} available")
        chosen = rng.sample(pool, quota)
        for orig_idx, rec in chosen:
            sampled.append(rec)
            meta_rows.append({
                "golden_index": orig_idx,
                "type": type_name,
                "file": rec.get("file", ""),
            })

    # Shuffle final set so type order is mixed
    combined = list(zip(sampled, meta_rows))
    rng.shuffle(combined)
    sampled, meta_rows = zip(*combined)

    # Write outputs
    json_path = out / "test_set_300.json"
    csv_path  = out / "test_set_300_meta.csv"

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(list(sampled), f, ensure_ascii=False, indent=2)

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["golden_index", "type", "file"])
        writer.writeheader()
        writer.writerows(meta_rows)

    print(f"Saved {len(sampled)} records → {json_path}")
    print(f"Meta  → {csv_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", default="data/ab_test/golden_clean.json")
    parser.add_argument("--out", default="data/ab_test")
    args = parser.parse_args()
    main(args.src, args.out)
