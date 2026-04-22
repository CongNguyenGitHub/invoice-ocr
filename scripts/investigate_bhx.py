"""Investigate bhx_2024 false positives and refine error detection."""
import json
import re
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")

with open("label_minitet_festive_24_v3_public.json", encoding="utf-8") as f:
    data = json.load(f)

bhx = [x for x in data if x.get("type") == "bhx_2024"]
print(f"bhx_2024 count: {len(bhx)}")

# Sample product names + codes
print("\n--- Sample bhx products ---")
for inv in bhx[:3]:
    receipt = inv.get("receipt_number")
    print(f"  receipt={receipt}")
    for p in inv.get("products", [])[:4]:
        code = p.get("product_code", "")
        name = p.get("product_name", "")
        print(f"    code={code!r}  name={name[:60]!r}")

# Show which tokens trigger digit_alpha_mid in bhx
pat = re.compile(r"(?<!\d)\b\d{4,}[A-Z]{2,}\b|\b[A-Z]{2,}\d{4,}\b")
print("\n--- bhx digit_alpha pattern hits (first 15) ---")
count = 0
for inv in bhx:
    for p in inv.get("products", []):
        code = p.get("product_code", "")
        name = p.get("product_name", "")
        for src in [code, name]:
            m = pat.search(src)
            if m:
                print(f"  match={m.group()!r}  in: {src[:70]!r}")
                count += 1
                break
    if count >= 15:
        break

# Also check lone_amp in bhx store names
print("\n--- bhx with lone_amp ---")
amp_pat = re.compile(r"(?<=\s)&(?=\s)")
for inv in bhx[:20]:
    name = inv.get("name", "")
    if amp_pat.search(name):
        print(f"  store: {name}")

# Check - is digit_alpha pattern actually valid product codes for bhx?
# Hypothesis: bhx uses barcode-style product_code like "8934563094812" (all numeric)
# Let's look at all bhx product_code patterns
code_patterns = Counter()
for inv in bhx:
    for p in inv.get("products", []):
        code = p.get("product_code", "")
        if not code:
            code_patterns["<empty>"] += 1
        elif code.isdigit():
            code_patterns["all_numeric"] += 1
        elif re.match(r"^[A-Z0-9]+$", code):
            code_patterns["alnum_upper"] += 1
        else:
            code_patterns["other"] += 1

print("\n--- bhx product_code pattern distribution ---")
for k, v in code_patterns.most_common():
    print(f"  {k}: {v}")
