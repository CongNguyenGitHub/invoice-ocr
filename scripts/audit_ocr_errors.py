"""
audit_ocr_errors.py — Refined OCR error audit for ground-truth labels.

Detects character corruption introduced by the OCR extraction phase.
Outputs:
  data/ab_test/golden_clean.json          – clean records only (all fields trusted)
  data/ab_test/audit_per_invoice.jsonl    – per-record audit metadata
  data/ab_test/audit_summary.json         – aggregate counts

Run:
    python scripts/audit_ocr_errors.py
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).parent.parent
SRC = ROOT / "label_minitet_festive_24_v3_public.json"
OUT_DIR = ROOT / "data" / "ab_test"
OUT_DIR.mkdir(parents=True, exist_ok=True)

with open(SRC, encoding="utf-8") as f:
    data = json.load(f)

print(f"Total invoices: {len(data)}")

# ─────────────────────────────────────────────────────────────────────────────
# Error detectors
# Each detector receives the full invoice dict and returns a set of error tags.
# ─────────────────────────────────────────────────────────────────────────────

# Helpers
_PAT_HASH3      = re.compile(r"###")
_PAT_HASH12     = re.compile(r"#{1,2}(?!#)")        # 1 or 2 hash, not triple
_PAT_QQQS       = re.compile(r"\?{3,}")
_PAT_REPL_CHAR  = re.compile(r"\ufffd")
_PAT_NONPRINT   = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_PAT_CONSCLUST  = re.compile(r"[bcdfghjklmnpqrstvwxyzBCDFGHJKLMNPQRSTVWXYZ]{6,}")


def _text_fields(inv: dict) -> list[tuple[str, str]]:
    """Return (field_label, value) for all human-readable text fields."""
    pairs: list[tuple[str, str]] = []
    for field in ("name", "cashier", "receipt_number", "pos_id"):
        v = inv.get(field, "")
        if v:
            pairs.append((field, v))
    for prod in inv.get("products", []):
        for pf in ("product_name", "product_id", "product_code"):
            v = prod.get(pf, "")
            if v:
                pairs.append((f"product.{pf}", v))
    return pairs


def _product_names(inv: dict) -> list[str]:
    return [p.get("product_name", "") for p in inv.get("products", []) if p.get("product_name")]


# ─── HARD error detectors (any hit → record is dirty, excluded from golden) ──

def detect_hash_placeholder(inv: dict) -> list[str]:
    """### or ##/# used as placeholder for unreadable text. Applies to ALL string fields."""
    errors = []
    # Check all fields, not just text_fields
    raw = json.dumps(inv, ensure_ascii=False)
    if "###" in raw:
        return ["hash_triple"]

    for _, val in _text_fields(inv):
        if _PAT_HASH12.search(val) and "###" not in val:
            errors.append("hash_single_double"); break
    return errors


def detect_question_placeholder(inv: dict) -> list[str]:
    """??? used as placeholder for unreadable text, or a single ? mid-word."""
    errors = []
    for _, val in _text_fields(inv):
        if _PAT_QQQS.search(val):
            errors.append("question_seq")
            break
        # Check for single ? mid-word (diacritic replacement)
        for word in val.split():
            core = word.strip(".,;:!()-[]\"'")
            if "?" in core and "???" not in core:
                if core == "?":
                     errors.append("mid_question_mark")
                     break
                for i, ch in enumerate(core):
                    if ch != "?":
                        continue
                        
                    left_ok = i > 0 and (core[i-1].isalpha() or core[i-1].isdigit())
                    right_ok = i < len(core)-1 and (core[i+1].isalpha() or core[i+1].isdigit())
                    
                    if left_ok or right_ok:
                        errors.append("mid_question_mark")
                        break
            if "mid_question_mark" in errors:
                break
    return errors


def detect_replacement_char(inv: dict) -> list[str]:
    """Unicode replacement character U+FFFD present."""
    for _, val in _text_fields(inv):
        if _PAT_REPL_CHAR.search(val):
            return ["replacement_char"]
    return []


def detect_non_printable(inv: dict) -> list[str]:
    """Raw control characters leaked into text."""
    for _, val in _text_fields(inv):
        if _PAT_NONPRINT.search(val):
            return ["non_printable"]
    return []


def detect_consonant_cluster(inv: dict) -> list[str]:
    """6+ consecutive consonants — strong signal of garbled OCR word.
    Exclusions:
      - product_id / product_code fields (barcodes, SKUs are legitimately all-caps alphanumeric)
      - Known abbreviation prefixes: TH, BIA, LON, etc. (handled by length ≥ 6 threshold)
    """
    for field, val in _text_fields(inv):
        # Skip ID/code fields — their format is intentionally alphanumeric-heavy
        if "id" in field or "code" in field:
            continue
        m = _PAT_CONSCLUST.search(val)
        if m:
            # Additional guard: many Vietnamese all-caps product abbreviations
            # are legitimate (e.g. "DDVSPN" in Lactacyd is a real SKU prefix).
            # Require the cluster to be inside a word surrounded by other text
            # AND the surrounding text to look garbled too.
            token = m.group()
            # If the whole field value IS the consonant cluster, it's garbled
            if len(val.strip()) <= len(token) + 3:
                return ["consonant_cluster_field"]
            # Otherwise flag only if full product name looks garbled overall
            # (heuristic: >40% of alpha chars are consonants, no Vietnamese vowel diacritics)
            alpha = re.sub(r"[^a-zA-Z]", "", val)
            if len(alpha) < 10:
                continue
            vowels = set("aeiouAEIOUăâêôơưàáảãạèéẻẽẹìíỉĩịòóỏõọùúủũụ")
            viet_vowel_count = sum(1 for c in val if c in vowels)
            if viet_vowel_count == 0 and len(alpha) >= 10:
                return ["consonant_cluster_garbled"]
    return []


def detect_numeric_field_corruption(inv: dict) -> list[str]:
    """Numeric metadata fields (total_money, dates) contain non-numeric garbage."""
    errors = []
    # total_money should be purely numeric (possibly empty)
    money = inv.get("total_money", "")
    if money and not re.match(r"^\d+(\.\d+)?$", money) and "#" in money:
        errors.append("total_money_corrupted")
    # date should match dd/mm/yyyy or be empty
    date = inv.get("date", "")
    if date and not re.match(r"^\d{1,2}/\d{1,2}/\d{4}$", date) and "#" in date:
        errors.append("date_corrupted")
    return errors


# All hard detectors in priority order
HARD_DETECTORS = [
    detect_hash_placeholder,
    detect_question_placeholder,
    detect_replacement_char,
    detect_non_printable,
    detect_consonant_cluster,
    detect_numeric_field_corruption,
]

# ─── SOFT flag detectors (informational, not used for exclusion) ──────────────

_PAT_LONE_AMP = re.compile(r"(?<=\s)&(?=\s)")


def flag_lone_amp(inv: dict) -> list[str]:
    """& surrounded by spaces — could be OCR artifact or legitimate (store names)."""
    for _, val in _text_fields(inv):
        if _PAT_LONE_AMP.search(val):
            return ["lone_amp"]
    return []


def flag_missing_critical_fields(inv: dict) -> list[str]:
    """Invoice is missing key metadata that makes it unusable for evaluation."""
    flags = []
    # Both date AND receipt_number empty → can't verify identity
    if not inv.get("date") and not inv.get("receipt_number"):
        flags.append("missing_date_and_receipt")
    # No products at all
    if not inv.get("products"):
        flags.append("no_products")
    # total_money absent
    if not inv.get("total_money"):
        flags.append("missing_total_money")
    return flags


def flag_empty_product_names(inv: dict) -> list[str]:
    """Any product with an empty product_name."""
    for prod in inv.get("products", []):
        if not prod.get("product_name", "").strip():
            return ["empty_product_name"]
    return []


SOFT_DETECTORS = [
    flag_lone_amp,
    flag_missing_critical_fields,
    flag_empty_product_names,
]

# ─────────────────────────────────────────────────────────────────────────────
# Main scan
# ─────────────────────────────────────────────────────────────────────────────

invoice_meta: list[dict] = []

for idx, inv in enumerate(data):
    hard_errors: list[str] = []
    soft_flags: list[str] = []

    for detector in HARD_DETECTORS:
        hard_errors.extend(detector(inv))
    for flagger in SOFT_DETECTORS:
        soft_flags.extend(flagger(inv))

    # Three-tier grading:
    #   dirty      = OCR corruption (hard errors) → always excluded
    #   incomplete = OCR clean but missing critical GT fields → excluded from golden
    #   clean      = usable as ground truth
    INCOMPLETE_FLAGS = {"missing_date_and_receipt", "missing_total_money", "empty_product_name"}
    if hard_errors:
        grade = "dirty"
    elif set(soft_flags) & INCOMPLETE_FLAGS:
        grade = "incomplete"
    else:
        grade = "clean"

    invoice_meta.append({
        "index": idx,
        "type": inv.get("type"),
        "file": inv.get("file"),
        "receipt_number": inv.get("receipt_number"),
        "hard_errors": list(set(hard_errors)),
        "soft_flags": list(set(soft_flags)),
        "grade": grade,
    })

# ─────────────────────────────────────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────────────────────────────────────

hard_cat_counts: Counter = Counter()
soft_cat_counts: Counter = Counter()
for m in invoice_meta:
    for e in m["hard_errors"]:
        hard_cat_counts[e] += 1
    for f in m["soft_flags"]:
        soft_cat_counts[f] += 1

n_dirty      = sum(1 for m in invoice_meta if m["grade"] == "dirty")
n_incomplete = sum(1 for m in invoice_meta if m["grade"] == "incomplete")
n_clean      = sum(1 for m in invoice_meta if m["grade"] == "clean")
n = len(data)

print("\n=== HARD error breakdown ===")
for cat, cnt in hard_cat_counts.most_common():
    print(f"  {cat:35s}: {cnt:4d}  ({cnt/n*100:.1f}%)")

print("\n=== SOFT flag breakdown (informational, not excluded) ===")
for cat, cnt in soft_cat_counts.most_common():
    print(f"  {cat:35s}: {cnt:4d}  ({cnt/n*100:.1f}%)")

print(f"\nDIRTY (OCR errors, excluded)       : {n_dirty:4d}  ({n_dirty/n*100:.1f}%)")
print(f"INCOMPLETE (missing GT, excluded)  : {n_incomplete:4d}  ({n_incomplete/n*100:.1f}%)")
print(f"CLEAN (golden, kept)               : {n_clean:4d}  ({n_clean/n*100:.1f}%)")

print("\n=== Per-type breakdown ===")
type_stats: dict[str, Counter] = defaultdict(Counter)
for m in invoice_meta:
    t = m["type"] or "UNKNOWN"
    type_stats[t][m["grade"]] += 1
    type_stats[t]["_total"] += 1

print(f"{'Type':12s}  {'Total':>6}  {'Clean':>6}  {'Incomp':>7}  {'Dirty':>6}  {'Clean%':>7}")
for t in sorted(type_stats):
    s = type_stats[t]
    total = s["_total"]
    clean = s["clean"]
    incomp = s["incomplete"]
    dirty = s["dirty"]
    pct = clean / total * 100 if total else 0
    print(f"  {t:12s}  {total:6d}  {clean:6d}  {incomp:7d}  {dirty:6d}  {pct:6.1f}%")

# ─────────────────────────────────────────────────────────────────────────────
# Save outputs
# ─────────────────────────────────────────────────────────────────────────────

# 1. golden_clean.json — all clean records
clean_records = [data[m["index"]] for m in invoice_meta if m["grade"] == "clean"]
golden_path = OUT_DIR / "golden_clean.json"
with open(golden_path, "w", encoding="utf-8") as f:
    json.dump(clean_records, f, ensure_ascii=False, indent=2)
print(f"\n[SAVED] Golden clean records  -> {golden_path}  ({len(clean_records)} records)")

# 2. audit_per_invoice.jsonl
meta_path = OUT_DIR / "audit_per_invoice.jsonl"
with open(meta_path, "w", encoding="utf-8") as f:
    for m in invoice_meta:
        f.write(json.dumps(m, ensure_ascii=False) + "\n")
print(f"[SAVED] Per-invoice audit     -> {meta_path}")

# 3. audit_summary.json
summary = {
    "total": n,
    "clean": n_clean,
    "dirty": n_dirty,
    "hard_error_breakdown": dict(hard_cat_counts.most_common()),
    "soft_flag_breakdown": dict(soft_cat_counts.most_common()),
    "per_type": {
        t: {
            "total": s["_total"],
            "clean": s["clean"],
            "incomplete": s["incomplete"],
            "dirty": s["dirty"],
        }
        for t, s in type_stats.items()
    },
}
summary_path = OUT_DIR / "audit_summary.json"
with open(summary_path, "w", encoding="utf-8") as f:
    json.dump(summary, f, ensure_ascii=False, indent=2)
print(f"[SAVED] Audit summary         -> {summary_path}")

print("\nDone.")
