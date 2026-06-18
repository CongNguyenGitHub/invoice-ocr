"""
schema_align.py — Align the dataset with the schema defined in src.schemas.invoice.InvoiceResult.
It will project out any extra fields not defined in the schema to ensure our ground truth directly
matches what the A/B evaluation expects.
"""

import argparse
import json
import sys
from pathlib import Path

# Add root to sys.path so we can import from src
root = Path(__file__).parent.parent
sys.path.insert(0, str(root))

from src.schemas.invoice import InvoiceResult


def main(src: str, out: str):
    print(f"Loading {src}...")
    with open(src, encoding="utf-8") as f:
        data = json.load(f)

    aligned_data = []

    invoice_schema_fields = InvoiceResult.model_fields.keys()
    # To get the product schema fields we need to inspect the inner type
    product_schema_fields = InvoiceResult.model_fields["products"].annotation.__args__[0].model_fields.keys()

    for item in data:
        # Keep only fields that exist in InvoiceResult
        aligned_item = {k: v for k, v in item.items() if k in invoice_schema_fields}

        # Explicitly keep the 'file' field for A/B testing image URLs
        if "file" in item:
            aligned_item["file"] = item["file"]

        # Handle the products list
        if "products" in aligned_item:
            aligned_products = []
            for prod in aligned_item["products"]:
                if isinstance(prod, dict):
                    aligned_prod = {k: v for k, v in prod.items() if k in product_schema_fields}
                    aligned_products.append(aligned_prod)
                else:
                    aligned_products.append(prod)
            aligned_item["products"] = aligned_products

        aligned_data.append(aligned_item)

    print("Checking alignment by validating against the pydantic schema...")
    for i, item in enumerate(aligned_data):
        try:
            # Create a copy without 'file' to test Pydantic validation (which forbids extra fields)
            test_item = {k: v for k, v in item.items() if k != "file"}
            InvoiceResult(**test_item)
        except Exception as e:
            print(f"Validation failed for record {i}: {e}")
            sys.exit(1)

    print("All records validated successfully against the schema.")

    with open(out, "w", encoding="utf-8") as f:
        json.dump(aligned_data, f, ensure_ascii=False, indent=2)

    print(f"Saved {len(aligned_data)} aligned records to -> {out}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", default="data/ab_test/test_set_300_normalized.json")
    parser.add_argument("--out", default="data/ab_test/test_set_300_aligned.json")
    args = parser.parse_args()
    main(args.src, args.out)
