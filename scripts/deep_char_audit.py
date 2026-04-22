"""
deep_char_audit.py — Exhaustive character-level corruption scan on golden_clean.json.

Detects single-char substitution patterns that slip past simple hash/question-block filters:
  - Single ? mid-word  (Vietnamese diacritic replacement: TA?O HO?NG)
  - Digit sandwiched between letters inside a product name  (garbled char)
  - ^ or ~ mid-word
  - | or \\ mid-word

FALSE POSITIVES deliberately excluded after investigation:
  - * in product names  → legitimate quantity multiplier (110ML*4, 40G*5)
  - @ prefix on product names → legitimate POS category separator
  - CamelCase cashier names → valid Vietnamese name without spaces

Output:
  data/ab_test/deep_char_audit.jsonl   — per-record findings
  data/ab_test/deep_char_patterns.json — pattern frequency table
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).parent.parent
SRC = ROOT / "data" / "ab_test" / "golden_clean.json"
OUT_DIR = ROOT / "data" / "ab_test"

with open(SRC, encoding="utf-8") as f:
    data = json.load(f)

print(f"Scanning {len(data)} records from golden_clean.json...\n")

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

WORD_RE = re.compile(r"\S+")

def _core(word: str) -> str:
    """Strip common trailing/leading punctuation for analysis."""
    return word.strip(".,;:!()[]\"'")


# ─────────────────────────────────────────────────────────────────────────────
# Word-level checkers
# ─────────────────────────────────────────────────────────────────────────────

def word_has_mid_question(word: str) -> bool:
    """? sandwiched mid-word = diacritic replacement.
    e.g.  TA?O  HO?NG  HA?T  Nươ?C  Đ?U
    NOT:  standalone trailing '?'  or  '???' (already caught as hard error).
    """
    c = _core(word)
    if "???" in c:
        return False
    for i, ch in enumerate(c):
        if ch != "?":
            continue
        left  = i > 0 and (c[i-1].isalpha() or c[i-1].isdigit())
        right = i < len(c)-1 and (c[i+1].isalpha() or c[i+1].isdigit())
        if left or right:
            return True
    return False


_DIGIT_MID = re.compile(r"[a-zA-ZÀ-ỹ]\d[a-zA-ZÀ-ỹ]")
# Legitimate patterns that contain letter-digit-letter but are NOT errors:
_LEGIT_DIGIT = re.compile(
    r"\d+[xX]\d+"             # 6X330, 24X10
    r"|\d+[xX][a-zA-Z]"       # 6Xgói
    r"|[a-zA-Z]+\d+[xX]"      # LOC6X
    r"|\d+[gGmMlLkK]+\*\d*"   # 180ML*4
    r"|^\d"                    # starts with digit: 330ML
)

def word_has_digit_mid_alpha(word: str) -> bool:
    """Single digit sandwiched between two letters = likely garbled char.
    Exclusions: measurement/pack formats, all-caps SKU codes ≤15 chars.
    """
    c = _core(word)
    if not c:
        return False
    # All-caps SKU / barcode codes are intentionally alphanumeric
    if re.match(r"^[A-Z0-9\-\_]+$", c) and len(c) <= 15:
        return False
    if _LEGIT_DIGIT.search(c):
        return False
    return bool(_DIGIT_MID.search(c))


def word_has_mid_caret(word: str) -> bool:
    """^ injected mid-word."""
    c = _core(word)
    if "^" not in c:
        return False
    i = c.index("^")
    return (i > 0 and c[i-1].isalpha()) or (i < len(c)-1 and c[i+1].isalpha())


def word_has_mid_tilde(word: str) -> bool:
    """~ injected mid-word."""
    c = _core(word)
    if "~" not in c:
        return False
    i = c.index("~")
    return (i > 0 and c[i-1].isalpha()) or (i < len(c)-1 and c[i+1].isalpha())


_PIPE_BACK = re.compile(r"[a-zA-ZÀ-ỹ][|\\][a-zA-ZÀ-ỹ]")

def word_has_pipe_backslash(word: str) -> bool:
    """| or \\ mid-word — almost never legitimate."""
    return bool(_PIPE_BACK.search(_core(word)))


WORD_CHECKERS: dict[str, callable] = {
    "mid_question_mark": word_has_mid_question,
    "digit_mid_alpha":   word_has_digit_mid_alpha,
    "mid_caret":         word_has_mid_caret,
    "mid_tilde":         word_has_mid_tilde,
    "pipe_backslash":    word_has_pipe_backslash,
}

# ─────────────────────────────────────────────────────────────────────────────
# Full-text patterns (not word-level)
# ─────────────────────────────────────────────────────────────────────────────

FULL_TEXT_PATTERNS: dict[str, re.Pattern] = {
    "backslash_n_literal": re.compile(r"\\n|\\r|\\t"),
    "unicode_escape":      re.compile(r"\\u[0-9a-fA-F]{4}"),
    "html_entity":         re.compile(r"&[a-zA-Z]{2,6};|&#\d+;"),
    "repeated_dotdot":     re.compile(r"\.{4,}"),
}

# ─────────────────────────────────────────────────────────────────────────────
# Scan every invoice
# ─────────────────────────────────────────────────────────────────────────────

TEXT_FIELDS    = ("name", "cashier", "receipt_number")
PRODUCT_FIELDS = ("product_name",)    # IDs/codes excluded — they're intentionally alphanumeric

hit_by_pattern: Counter = Counter()
record_hits: list[dict] = []

for idx, inv in enumerate(data):
    inv_errors: dict[str, list[str]] = defaultdict(list)

    texts: list[tuple[str, str]] = []
    for fld in TEXT_FIELDS:
        v = inv.get(fld, "")
        if v:
            texts.append((fld, v))
    for prod in inv.get("products", []):
        for pfld in PRODUCT_FIELDS:
            v = prod.get(pfld, "")
            if v:
                texts.append((f"product.{pfld}", v))

    for field_label, text in texts:
        # Word-level
        for word in WORD_RE.findall(text):
            for pat_name, checker in WORD_CHECKERS.items():
                if checker(word):
                    inv_errors[pat_name].append(f"[{field_label}] {text[:80]}")
                    break

        # Full-text
        for pat_name, regex in FULL_TEXT_PATTERNS.items():
            if regex.search(text):
                if len(inv_errors[pat_name]) == 0:
                    inv_errors[pat_name].append(f"[{field_label}] {text[:80]}")

    if inv_errors:
        for pat_name in inv_errors:
            hit_by_pattern[pat_name] += 1
        record_hits.append({
            "index": idx,
            "type": inv.get("type"),
            "file": inv.get("file"),
            "errors": {k: v[:2] for k, v in inv_errors.items()},
        })

# ─────────────────────────────────────────────────────────────────────────────
# Report
# ─────────────────────────────────────────────────────────────────────────────

n = len(data)
print("=== Per-pattern hit counts ===")
for pat, cnt in hit_by_pattern.most_common():
    mark = "⚠" if cnt / n > 0.005 else " "
    print(f"  {mark} {pat:28s}: {cnt:4d}  ({cnt/n*100:.2f}%)")

n_any = len(record_hits)
print(f"\nNewly dirty (any deep error)   : {n_any:4d}  ({n_any/n*100:.1f}%)")
print(f"Remaining clean after deep scan: {n-n_any:4d}  ({(n-n_any)/n*100:.1f}%)")

print("\n=== Per-type breakdown ===")
type_new_dirty: Counter = Counter(r["type"] for r in record_hits)
type_totals: Counter = Counter(inv.get("type") for inv in data)
for t in sorted(type_totals):
    total = type_totals[t]
    nd = type_new_dirty.get(t, 0)
    print(f"  {t:12s}: {nd:3d} / {total:3d} newly dirty  ({nd/total*100:.1f}%)")

print("\n=== Examples per pattern (first 5) ===")
pat_examples: dict[str, list[str]] = defaultdict(list)
for r in record_hits:
    for pat, examples in r["errors"].items():
        if len(pat_examples[pat]) < 5:
            pat_examples[pat].extend(examples[:1])

for pat in sorted(pat_examples):
    print(f"\n-- {pat} --")
    for e in pat_examples[pat]:
        print(f"   {e}")

# ─────────────────────────────────────────────────────────────────────────────
# Save outputs
# ─────────────────────────────────────────────────────────────────────────────

out_audit = OUT_DIR / "deep_char_audit.jsonl"
with open(out_audit, "w", encoding="utf-8") as f:
    for r in record_hits:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")
print(f"\n[SAVED] Deep char audit -> {out_audit}  ({len(record_hits)} records)")

out_patterns = OUT_DIR / "deep_char_patterns.json"
with open(out_patterns, "w", encoding="utf-8") as f:
    json.dump({
        "total_golden": n,
        "newly_dirty": n_any,
        "remaining_clean": n - n_any,
        "pattern_counts": dict(hit_by_pattern.most_common()),
    }, f, ensure_ascii=False, indent=2)
print(f"[SAVED] Pattern summary -> {out_patterns}")
