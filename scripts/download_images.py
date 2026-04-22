"""
Invoice OCR — Batch Image Downloader

Downloads receipt images from the labeled dataset for offline evaluation.
Images are cached locally to avoid re-downloading.

Usage:
    python scripts/download_images.py
    python scripts/download_images.py --output data/eval_images --workers 10
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx


def download_one(url: str, output_dir: Path) -> tuple[str, bool, str]:
    """Download a single image. Returns (url, success, message)."""
    filename = url.split("/")[-1]
    if not filename.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
        filename += '.jpg'
    output_path = output_dir / filename

    if output_path.exists():
        return (url, True, "cached")

    try:
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
            output_path.write_bytes(resp.content)
            return (url, True, f"{len(resp.content) / 1024:.0f}KB")
    except Exception as e:
        return (url, False, str(e))


def main():
    parser = argparse.ArgumentParser(description="Download evaluation images")
    parser.add_argument("--input", default="label_minitet_festive_24_v3_public.json")
    parser.add_argument("--output", default="data/eval_images")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    # Load labels
    with open(args.input, 'r', encoding='utf-8') as f:
        labels = json.load(f)

    urls = [r["file"] for r in labels if r.get("file")]
    print(f"Total images to download: {len(urls)}")

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Download in parallel
    success = 0
    failed = 0
    cached = 0

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(download_one, url, output_dir): url for url in urls}
        for i, future in enumerate(as_completed(futures)):
            url, ok, msg = future.result()
            if ok:
                if msg == "cached":
                    cached += 1
                else:
                    success += 1
            else:
                failed += 1
                print(f"  FAILED: {url[-50:]} — {msg}")

            if (i + 1) % 100 == 0:
                print(f"  Progress: {i+1}/{len(urls)} (new={success}, cached={cached}, failed={failed})")

    print(f"\nDone! Downloaded={success}, Cached={cached}, Failed={failed}")
    print(f"Images saved to: {output_dir}")


if __name__ == "__main__":
    main()
