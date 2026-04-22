"""
rebuild_golden.py — Merge deep_char_audit.jsonl findings back into the main audit
and regenerate golden_clean.json with all errors filtered out.

Run AFTER:
  1. python scripts/audit_ocr_errors.py
  2. python scripts/deep_char_audit.py

This script reads audit_per_invoice.jsonl (from step 1) and deep_char_audit.jsonl
(from step 2), marks newly-found dirty records, and exports the final golden set.
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / "data" / "ab_test"
SRC = ROOT / "label_minitet_festive_24_v3_public.json"

# Load original full dataset
with open(SRC, encoding="utf-8") as f:
    raw_data = json.load(f)

# Load phase-1 audit (OCR hard errors + incomplete)
with open(DATA_DIR / "audit_per_invoice.jsonl", encoding="utf-8") as f:
    phase1 = {json.loads(line)["index"]: json.loads(line) for line in f}

# Load phase-2 deep char audit (character substitution errors)
with open(DATA_DIR / "deep_char_audit.jsonl", encoding="utf-8") as f:
    phase2_dirty: set[int] = set()
    phase2_errors: dict[int, dict] = {}
    for line in f:
        rec = json.loads(line)
        # phase2 index is relative to golden_clean.json — need to map back to raw
        # The golden_clean.json was built from clean phase1 records only.
        # Rebuild the mapping: golden_idx -> raw_idx
        pass  # we'll build the mapping below

# Rebuild mapping: golden_clean.json index -> raw_data index
phase1_clean_raw_indices = [
    m["index"] for m in phase1.values() if m["grade"] == "clean"
]
# (they were written in order, so position = golden index)
golden_to_raw = {golden_idx: raw_idx for golden_idx, raw_idx in enumerate(phase1_clean_raw_indices)}

with open(DATA_DIR / "deep_char_audit.jsonl", encoding="utf-8") as f:
    for line in f:
        rec = json.loads(line)
        golden_idx = rec["index"]
        raw_idx = golden_to_raw.get(golden_idx)
        if raw_idx is not None:
            phase2_dirty.add(raw_idx)
            phase2_errors[raw_idx] = rec["errors"]

print(f"Phase-1 audit records    : {len(phase1)}")
print(f"Phase-2 newly dirty (raw): {len(phase2_dirty)}")

# ─────────────────────────────────────────────────────────────────────────────
# Build final grades
# ─────────────────────────────────────────────────────────────────────────────

final_meta: list[dict] = []
grade_counter: Counter = Counter()

for raw_idx, p1 in phase1.items():
    if p1["grade"] == "dirty":
        final_grade = "dirty"
        additional = {}
    elif p1["grade"] == "incomplete":
        final_grade = "incomplete"
        additional = {}
    elif raw_idx in phase2_dirty:
        final_grade = "dirty_deep"   # clean at OCR level but char-substitution error
        additional = {"deep_char_errors": phase2_errors[raw_idx]}
    else:
        final_grade = "golden"
        additional = {}

    grade_counter[final_grade] += 1
    final_meta.append({
        "index": raw_idx,
        "type": p1["type"],
        "file": p1["file"],
        "receipt_number": p1.get("receipt_number"),
        "final_grade": final_grade,
        "hard_errors": p1["hard_errors"],
        "soft_flags": p1["soft_flags"],
        **additional,
    })

# Sort by raw index for consistency
final_meta.sort(key=lambda m: m["index"])

# ─────────────────────────────────────────────────────────────────────────────
# Save golden_clean.json (golden only)
# ─────────────────────────────────────────────────────────────────────────────

golden_records = [raw_data[m["index"]] for m in final_meta if m["final_grade"] == "golden"]

out_golden = DATA_DIR / "golden_clean.json"
with open(out_golden, "w", encoding="utf-8") as f:
    json.dump(golden_records, f, ensure_ascii=False, indent=2)

# ─────────────────────────────────────────────────────────────────────────────
# Save updated audit
# ─────────────────────────────────────────────────────────────────────────────

out_final_meta = DATA_DIR / "final_audit.jsonl"
with open(out_final_meta, "w", encoding="utf-8") as f:
    for m in final_meta:
        f.write(json.dumps(m, ensure_ascii=False) + "\n")

# ─────────────────────────────────────────────────────────────────────────────
# Report
# ─────────────────────────────────────────────────────────────────────────────

n = len(raw_data)
print(f"\n=== Final grade distribution (out of {n} original invoices) ===")
grade_labels = {
    "golden":      "✅ GOLDEN (usable for A/B test)",
    "incomplete":  "⚠️  INCOMPLETE (missing critical GT fields)",
    "dirty":       "❌ DIRTY (OCR hard error: ###, ???, etc.)",
    "dirty_deep":  "❌ DIRTY_DEEP (char substitution: ?, digit-mid-alpha)",
}
for grade in ["golden", "incomplete", "dirty", "dirty_deep"]:
    cnt = grade_counter[grade]
    print(f"  {grade_labels[grade]:50s}: {cnt:4d}  ({cnt/n*100:.1f}%)")

print(f"\nTotal excluded: {n - grade_counter['golden']}  ({(n-grade_counter['golden'])/n*100:.1f}%)")
print(f"Golden set    : {grade_counter['golden']}  ({grade_counter['golden']/n*100:.1f}%)")

# Per-type breakdown
print("\n=== Per-type golden count ===")
type_stats: dict[str, Counter] = defaultdict(Counter)
for m in final_meta:
    type_stats[m["type"]][m["final_grade"]] += 1
    type_stats[m["type"]]["_total"] += 1

print(f"{'Type':12s}  {'Total':>6}  {'Golden':>7}  {'Incompl':>7}  {'Dirty':>7}  {'Golden%':>7}")
for t in sorted(type_stats):
    s = type_stats[t]
    total   = s["_total"]
    golden  = s["golden"]
    incompl = s["incomplete"]
    dirty   = s["dirty"] + s["dirty_deep"]
    pct     = golden / total * 100
    print(f"  {t:12s}  {total:6d}  {golden:7d}  {incompl:7d}  {dirty:7d}  {pct:6.1f}%")

print(f"\n[SAVED] golden_clean.json -> {out_golden}  ({len(golden_records)} records)")
print(f"[SAVED] final_audit.jsonl -> {out_final_meta}")
