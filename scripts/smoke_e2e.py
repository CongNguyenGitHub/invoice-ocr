"""End-to-end smoke test against real ground-truth receipts.

Downloads N images from the public ground-truth, submits each to
POST /v1/receipts (or polls /v1/receipts/{id} on 504), then compares
extracted fields with the labels.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen

import requests


def _download(url: str) -> bytes:
    req = Request(url, headers={"User-Agent": "invoice-ocr-smoke/1.0"})
    with urlopen(req, timeout=20) as r:
        return r.read()


def _submit(api: str, raw: bytes) -> tuple[int, dict]:
    r = requests.post(
        f"{api}/v1/receipts",
        files={"file": ("receipt.jpg", raw, "image/jpeg")},
        timeout=120,
    )
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"raw": r.text[:200]}


def _poll(api: str, job_id: str, max_wait: int) -> tuple[int, dict]:
    deadline = time.time() + max_wait
    while time.time() < deadline:
        r = requests.get(f"{api}/v1/receipts/{job_id}", timeout=10)
        if r.status_code != 202:
            return r.status_code, r.json()
        time.sleep(2)
    return r.status_code, r.json()


def _compare(label: dict, got: dict) -> dict:
    keys = ("name", "date", "time", "pos_id", "receipt_number",
            "cashier", "total_money", "barcode")
    diffs = {}
    for k in keys:
        l = (label.get(k) or "").strip()
        g = (got.get(k) or "").strip()
        if l != g:
            diffs[k] = {"label": l, "got": g}
    label_n = len(label.get("products") or [])
    got_n = len(got.get("products") or [])
    if label_n != got_n:
        diffs["products_count"] = {"label": label_n, "got": got_n}
    return diffs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--ground-truth",
                    default="label_minitet_festive_24_v3_public.json")
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument("--max-wait", type=int, default=180)
    args = ap.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

    labels = json.loads(Path(args.ground_truth).read_text(encoding="utf-8"))
    sample = labels[: args.n]

    summary = {"total": 0, "pass_strict": 0, "diffs": []}
    for i, label in enumerate(sample):
        url = label["file"]
        print(f"\n[{i+1}/{len(sample)}] {label.get('type')} — {url}")
        try:
            raw = _download(url)
        except Exception as e:
            print(f"  ! download failed: {e}")
            continue

        t0 = time.time()
        status, body = _submit(args.api, raw)
        elapsed = time.time() - t0
        print(f"  POST {status} in {elapsed:.2f}s")

        if status == 504 and "job_id" in body:
            jid = body["job_id"]
            print(f"  polling {jid} ...")
            status, body = _poll(args.api, jid, args.max_wait)
            print(f"  GET {status}")

        summary["total"] += 1
        if status != 200:
            summary["diffs"].append({"i": i, "http": status, "body": body})
            continue

        diffs = _compare(label, body)
        if not diffs:
            summary["pass_strict"] += 1
            print("  ✓ exact match on all top-level fields + product count")
        else:
            summary["diffs"].append({"i": i, "http": 200, "diffs": diffs})
            for k, v in diffs.items():
                print(f"  ~ {k}: label={v['label']!r}  got={v['got']!r}")

    print("\n=== SUMMARY ===")
    print(f"submitted: {summary['total']}")
    print(f"strict-match: {summary['pass_strict']}/{summary['total']}")
    Path("smoke_report.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("report: smoke_report.json")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
