"""Trace remaining ### records in golden_clean.json."""
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).parent.parent
with open(ROOT / "data" / "ab_test" / "golden_clean.json", encoding="utf-8") as f:
    data = json.load(f)

issues = []
for idx, inv in enumerate(data):
    raw = json.dumps(inv, ensure_ascii=False)
    if "###" not in raw:
        continue
    hits = {}
    for k, v in inv.items():
        if isinstance(v, str) and "###" in v:
            hits[k] = v
    for prod in inv.get("products", []):
        for pk, pv in prod.items():
            if isinstance(pv, str) and "###" in pv:
                hits["product." + pk] = pv[:60]
    issues.append({"golden_idx": idx, "type": inv.get("type"), "hits": hits})

print(f"Records with ### still in golden_clean.json: {len(issues)}")
for r in issues:
    gidx = r["golden_idx"]
    t = r["type"]
    print(f"\n  golden_idx={gidx}  type={t}")
    for k, v in r["hits"].items():
        print(f"    {k}: {v[:80]}")
