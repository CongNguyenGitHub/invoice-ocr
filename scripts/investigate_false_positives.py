"""
Investigate mid_asterisk and random_uppercase false positives
before updating the final audit rules.
"""
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).parent.parent

with open(ROOT / "data" / "ab_test" / "deep_char_audit.jsonl", encoding="utf-8") as f:
    dirty = [json.loads(line) for line in f]

with open(ROOT / "data" / "ab_test" / "golden_clean.json", encoding="utf-8") as f:
    data = json.load(f)

# ── Investigate mid_asterisk ────────────────────────────────────────────────
asterisk_hits = [r for r in dirty if "mid_asterisk" in r["errors"]]
print(f"mid_asterisk records: {len(asterisk_hits)}")

# Sample 20 product names that contain *
all_star_names = []
for r in asterisk_hits:
    inv = data[r["index"]]
    for prod in inv.get("products", []):
        name = prod.get("product_name", "")
        if "*" in name:
            all_star_names.append((r["type"], name))

print(f"\nAll unique * patterns found ({len(all_star_names)} total):")
# Categorize: trailing * vs mid * vs * with digits (multiplier)
multiplier_re  = re.compile(r"\d+[Gg][xX*]\d+|\d+[Mm][Ll][xX*]\d+|\w+[*]\d+|\d+[*]\w+|[*]\d+$|\d+[*]$|\w+\*\d*$")
true_subst_re  = re.compile(r"[a-zA-ZÀ-ỹ]\*[a-zA-ZÀ-ỹ]")  # letter*letter = possible subst

category_counter = Counter()
true_subst_examples = []
multiplier_examples = []

for t, name in all_star_names:
    if true_subst_re.search(name):
        category_counter["letter*letter (possible substitution)"] += 1
        true_subst_examples.append(f"  [{t}] {name}")
    elif multiplier_re.search(name):
        category_counter["multiplier/quantity (legit format)"] += 1
        multiplier_examples.append(f"  [{t}] {name[:70]}")
    else:
        category_counter["other"] += 1

print("\nCategory breakdown:")
for k, v in category_counter.most_common():
    print(f"  {k}: {v}")

print("\nExamples — letter*letter (substitution, true errors):")
for e in true_subst_examples[:10]:
    print(e)

print("\nExamples — multiplier (legit, should NOT be excluded):")
for e in multiplier_examples[:10]:
    print(e)

# ── Investigate random_uppercase ─────────────────────────────────────────────
upper_hits = [r for r in dirty if "random_uppercase" in r["errors"]]
print(f"\n\nrandom_uppercase records: {len(upper_hits)}")
print("Cashier name examples:")
for r in upper_hits[:10]:
    inv = data[r["index"]]
    cashier = inv.get("cashier", "")
    print(f"  [{inv.get('type')}] cashier={cashier!r}")

# ── Investigate digit_mid_alpha ───────────────────────────────────────────────
digit_hits = [r for r in dirty if "digit_mid_alpha" in r["errors"]]
print(f"\n\ndigit_mid_alpha records: {len(digit_hits)}")
print("Product name examples:")
for r in digit_hits[:10]:
    inv = data[r["index"]]
    for prod in inv.get("products", []):
        name = prod.get("product_name", "")
        if re.search(r"[a-zA-ZÀ-ỹ]\d[a-zA-ZÀ-ỹ]", name):
            print(f"  [{inv.get('type')}] {name}")

# ── Investigate mid_at_sign ──────────────────────────────────────────────────
at_hits = [r for r in dirty if "mid_at_sign" in r["errors"]]
print(f"\n\nmid_at_sign records: {len(at_hits)}")
for r in at_hits[:5]:
    inv = data[r["index"]]
    for prod in inv.get("products", []):
        name = prod.get("product_name", "")
        if "@" in name:
            print(f"  [{inv.get('type')}] {name}")

# ── Investigate mid_question_mark (all instances) ────────────────────────────
q_hits = [r for r in dirty if "mid_question_mark" in r["errors"]]
print(f"\n\nmid_question_mark records: {len(q_hits)}")
for r in q_hits:
    inv = data[r["index"]]
    for prod in inv.get("products", []):
        name = prod.get("product_name", "")
        if re.search(r"[a-zA-ZÀ-ỹ]\?[a-zA-ZÀ-ỹ0-9]|[a-zA-ZÀ-ỹ0-9]\?[a-zA-ZÀ-ỹ]", name):
            print(f"  [{inv.get('type')}] {name}")
