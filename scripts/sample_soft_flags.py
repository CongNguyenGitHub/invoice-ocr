"""Sample soft-flagged records to help decide inclusion policy."""
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).parent.parent
with open(ROOT / "data" / "ab_test" / "audit_per_invoice.jsonl", encoding="utf-8") as f:
    meta = [json.loads(line) for line in f]

with open(ROOT / "label_minitet_festive_24_v3_public.json", encoding="utf-8") as f:
    data = json.load(f)

flags_to_show = [
    "missing_date_and_receipt",
    "missing_total_money",
    "empty_product_name",
    "lone_amp",
]

for flag in flags_to_show:
    hits = [m for m in meta if flag in m["soft_flags"] and m["grade"] == "clean"]
    print(f"--- {flag}: {len(hits)} records ---")
    for m in hits[:3]:
        inv = data[m["index"]]
        t = inv.get("type")
        date = repr(inv.get("date"))
        receipt = repr(inv.get("receipt_number"))
        total = repr(inv.get("total_money"))
        nprods = len(inv.get("products", []))
        print(f"  idx={m['index']} type={t}  date={date}  receipt={receipt}  total={total}  n_products={nprods}")
    print()

# Also count overlaps: records with BOTH missing_date_and_receipt AND missing_total_money
both = [m for m in meta if
        "missing_date_and_receipt" in m["soft_flags"] and
        "missing_total_money" in m["soft_flags"] and
        m["grade"] == "clean"]
print(f"Records with BOTH missing_date_and_receipt AND missing_total_money: {len(both)}")
only_missing_total = [m for m in meta if
    "missing_total_money" in m["soft_flags"] and
    "missing_date_and_receipt" not in m["soft_flags"] and
    m["grade"] == "clean"]
print(f"Records with ONLY missing_total_money (date/receipt is present): {len(only_missing_total)}")
