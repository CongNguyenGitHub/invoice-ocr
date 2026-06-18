"""
normalize_dataset.py — Normalize text fields in the dataset to improve evaluation accuracy.
Applies constraints like:
 - Removes Vietnamese diacritics (accents)
 - Lowercases all text
 - Normalizes whitespace (removes leading/trailing, merges multiple spaces)
"""

import argparse
import json
import re
import unicodedata


def remove_accents(input_str: str) -> str:
    """Removes combining diacritical marks and handles special Vietnamese chars."""
    # Normalize to NFD (decomposition) which separates characters from their diacritics
    nfd_form = unicodedata.normalize("NFD", input_str)
    # Remove combining characters (diacritics)
    only_ascii = "".join([c for c in nfd_form if not unicodedata.combining(c)])
    # We also need to handle 'đ' and 'Đ' manually as they are not decomposed by NFD
    return only_ascii.replace("đ", "d").replace("Đ", "d").replace("Ð", "d")  # Ð is some variant


def normalize_text(text: str) -> str:
    if not text:
        return text
    # Remove accents
    text = remove_accents(text)
    # Lowercase
    text = text.lower()
    # Normalize spaces
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize_record(rec: dict) -> dict:
    """Recursively normalizes string values within a dictionary."""
    norm_rec = {}
    for k, v in rec.items():
        # Do not normalize specific keys that might be sensitive, though usually fine.
        # e.g., 'type', 'file' we can leave intact or just normalize everything.
        if k in ("file", "type"):
            norm_rec[k] = v
            continue

        if isinstance(v, str):
            norm_rec[k] = normalize_text(v)
        elif isinstance(v, list):
            new_list = []
            for item in v:
                if isinstance(item, dict):
                    new_list.append(normalize_record(item))
                elif isinstance(item, str):
                    new_list.append(normalize_text(item))
                else:
                    new_list.append(item)
            norm_rec[k] = new_list
        elif isinstance(v, dict):
            norm_rec[k] = normalize_record(v)
        else:
            norm_rec[k] = v
    return norm_rec


def main(src: str, out: str):
    print(f"Loading {src}...")
    with open(src, encoding="utf-8") as f:
        data = json.load(f)

    print("Normalizing records (removing diacritics, lowercasing, space fixing)...")
    norm_data = [normalize_record(inv) for inv in data]

    with open(out, "w", encoding="utf-8") as f:
        json.dump(norm_data, f, ensure_ascii=False, indent=2)

    print(f"Saved {len(norm_data)} normalized records to -> {out}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", default="data/ab_test/test_set_300.json")
    parser.add_argument("--out", default="data/ab_test/test_set_300_normalized.json")
    args = parser.parse_args()
    main(args.src, args.out)
