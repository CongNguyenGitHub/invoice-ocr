"""Quick sanity check on the final golden_clean.json."""

import json
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).parent.parent
with open(ROOT / "data" / "ab_test" / "golden_clean.json", encoding="utf-8") as f:
    data = json.load(f)

print(f"Total golden records: {len(data)}")

# Check all known error patterns are gone
checks = {
    "mid-? in product_name": sum(
        1
        for inv in data
        for p in inv.get("products", [])
        for pn in [p.get("product_name", "")]
        if any(
            c == "?"
            and (
                (i > 0 and (pn[i - 1].isalpha() or pn[i - 1].isdigit()))
                or (i < len(pn) - 1 and (pn[i + 1].isalpha() or pn[i + 1].isdigit()))
            )
            for i, c in enumerate(pn)
        )
    ),
    "### anywhere": sum(1 for inv in data if "###" in json.dumps(inv, ensure_ascii=False)),
    "??? anywhere": sum(1 for inv in data if "???" in json.dumps(inv, ensure_ascii=False)),
}

for label, cnt in checks.items():
    status = "✅ CLEAR" if cnt == 0 else f"⚠️  STILL {cnt} found"
    print(f"  {label:30s}: {status}")

types = Counter(inv.get("type") for inv in data)
print("\nType distribution in golden set:")
for t, cnt in sorted(types.items(), key=lambda x: -x[1]):
    print(f"  {t:12s}: {cnt}")
