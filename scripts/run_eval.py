"""
run_eval.py -- HTTP-based batch evaluation runner.

Calls the LIVE API (POST /v1/receipts) for every record in a split file,
then compares the result against the ground-truth label using the same
field-comparison and greedy product-alignment logic as evaluate.py.

Why HTTP instead of run_pipeline() directly?
  • Eval must match production exactly: YOLO crop, Triton batch, whitelist
    post-processing, pHash cache -- all of these only happen in the real stack.
  • Allows running evals against any deployed environment, not just localhost.

Output
──────
  eval_reports/eval_report_{psv}_{split}_{timestamp}.json

    {
      "psv":   "v3.5",
      "split": "dev",
      "input": "data/eval/dev_set.json",
      "n_total": 950, "n_success": 948, "n_failed": 2,
      "avg_latency_s": 3.8,
      "field_accuracy": {
        "name": {"correct": 900, "total": 948, "pct": 94.9},
        ...
      },
      "by_type": {
        "aeon": {"n": 112, "field_accuracy": {...}},
        ...
      },
      "per_record": [
        {"orig_file": "...", "type": "aeon",
         "status": "ok"|"failed"|"api_error",
         "latency_s": 3.2,
         "mismatches": [{"field": "name", "expected": "...", "predicted": "..."}]}
      ]
    }

Usage
─────
    # Full dev eval
    python scripts/run_eval.py \\
        --input data/eval/dev_set.json \\
        --psv v3.5 \\
        --split dev \\
        --api http://localhost:8000 \\
        --workers 4 \\
        --out eval_reports/

    # Quick 20-sample smoke
    python scripts/run_eval.py --input data/eval/dev_set.json --sample 20
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import httpx

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

POLL_INTERVAL  = 2.0   # s between polls
POLL_MAX_WAIT  = 120.0 # s max poll time per job


# ── Field comparison helpers (inlined — no src.* deps) ───────────────────────

import re
import difflib


def _normalize_money_for_compare(value: str) -> str:
    if not value:
        return ""
    v = re.sub(r'[^\d]', '', str(value))
    return v.lstrip('0') or '0'


def _normalize_quantity(q: str) -> str:
    """Normalise product quantity to a canonical numeric string."""
    if not q:
        return ""
    q = str(q).strip().lower()
    q = re.sub(r'[^\d.,]', '', q)
    q = q.replace(',', '.')
    try:
        return str(float(q)).rstrip('0').rstrip('.')
    except ValueError:
        return q


def _normalize_label_record(record: dict) -> dict:
    products = []
    for p in record.get("products", []):
        products.append({
            "product_id":          p.get("product_id", p.get("product_code", "")),
            "product_name":        p.get("product_name", ""),
            "product_unit_price":  p.get("product_unit_price", ""),
            "product_quantity":    p.get("product_quantity", p.get("product_amount", "")),
            "product_total_money": p.get("product_total_money", ""),
        })
    return {
        "name":           record.get("name", ""),
        "type":           record.get("type", ""),
        "date":           record.get("date", ""),
        "time":           record.get("time", ""),
        "pos_id":         record.get("pos_id", ""),
        "receipt_number": record.get("receipt_number", ""),
        "cashier":        record.get("cashier", ""),
        "total_money":    record.get("total_money", ""),
        "barcode":        record.get("barcode", ""),
        "products":       products,
    }


def compare_field(pred: str, gt: str, field_name: str) -> bool | None:
    if not gt:
        return None
    if not pred:
        return False
    pred = str(pred).strip().lower()
    gt   = str(gt).strip().lower()
    if field_name in ("total_money", "product_unit_price", "product_total_money"):
        return _normalize_money_for_compare(pred) == _normalize_money_for_compare(gt)
    if field_name == "barcode":
        return pred.replace('*', '') == gt.replace('*', '')
    if field_name == "product_quantity":
        return _normalize_quantity(pred) == _normalize_quantity(gt)
    if field_name == "product_name":
        return difflib.SequenceMatcher(None, pred, gt).ratio() > 0.95
    return pred == gt


def evaluate_single(pred: dict, gt: dict) -> tuple[dict[str, list[bool]], list[dict]]:
    results: dict[str, list[bool]] = {}
    mismatches: list[dict] = []

    base_fields = ["name", "type", "date", "time", "pos_id",
                   "receipt_number", "cashier", "total_money", "barcode"]
    for fn in base_fields:
        val = compare_field(pred.get(fn, ""), gt.get(fn, ""), fn)
        if val is not None:
            results.setdefault(fn, []).append(val)
            if not val:
                mismatches.append({"field": fn,
                                   "expected":  str(gt.get(fn, "")).strip(),
                                   "predicted": str(pred.get(fn, "")).strip()})

    gt_products   = gt.get("products", [])
    raw_pred_prods = pred.get("products", [])

    # Greedy alignment
    aligned_pred: list[dict] = []
    used: set[int] = set()
    for gt_p in gt_products:
        gt_price = _normalize_money_for_compare(gt_p.get("product_unit_price", ""))
        gt_total = _normalize_money_for_compare(gt_p.get("product_total_money", ""))
        gt_name  = str(gt_p.get("product_name", "")).strip().lower()
        best_idx, best_score = -1, -1
        for pi, pp in enumerate(raw_pred_prods):
            if pi in used:
                continue
            score = 0
            if gt_price and _normalize_money_for_compare(pp.get("product_unit_price","")) == gt_price: score += 2
            if gt_total and _normalize_money_for_compare(pp.get("product_total_money","")) == gt_total: score += 2
            if gt_name  and str(pp.get("product_name","")).strip().lower() == gt_name: score += 3
            if score > best_score:
                best_score, best_idx = score, pi
        if best_idx != -1 and best_score > 0:
            aligned_pred.append(raw_pred_prods[best_idx])
            used.add(best_idx)
        else:
            aligned_pred.append({})
    for pi, pp in enumerate(raw_pred_prods):
        if pi not in used:
            aligned_pred.append(pp)

    product_fields = ["product_id", "product_name", "product_unit_price",
                      "product_quantity", "product_total_money"]
    for i in range(max(len(gt_products), len(aligned_pred))):
        gt_p   = gt_products[i]   if i < len(gt_products)   else {}
        pred_p = aligned_pred[i]  if i < len(aligned_pred)  else {}
        for fn in product_fields:
            val = compare_field(pred_p.get(fn, ""), gt_p.get(fn, ""), fn)
            if val is not None:
                results.setdefault(fn, []).append(val)
                if not val:
                    mismatches.append({"field":     f"product[{i}].{fn}",
                                       "expected":  str(gt_p.get(fn,"")).strip(),
                                       "predicted": str(pred_p.get(fn,"")).strip()})
    return results, mismatches


# ─── Image download (with local cache) ───────────────────────────────────────

def _download(url: str, cache_dir: Path) -> bytes | None:
    filename = url.split("/")[-1]
    cache_path = cache_dir / filename
    if cache_path.exists():
        return cache_path.read_bytes()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "eval/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
        cache_path.write_bytes(data)
        return data
    except Exception as exc:
        log.warning("Download failed %s: %s", url, exc)
        return None


# ─── Single record evaluation ────────────────────────────────────────────────

def _eval_one(
    record: dict,
    api_base: str,
    cache_dir: Path,
    client: httpx.Client,
) -> dict:
    url = record.get("file", "")
    t0  = time.perf_counter()

    img = _download(url, cache_dir)
    if img is None:
        return {"orig_file": url, "type": record.get("type", ""),
                "status": "download_failed", "latency_s": 0.0, "mismatches": []}

    # Submit
    try:
        resp = client.post(
            f"{api_base}/v1/receipts",
            files={"file": ("receipt.jpg", img, "image/jpeg")},
            timeout=90.0,
        )
    except Exception as exc:
        return {"orig_file": url, "type": record.get("type", ""),
                "status": "api_error", "error": str(exc),
                "latency_s": time.perf_counter() - t0, "mismatches": []}

    http_code = resp.status_code

    # Synchronous 200 -- bare InvoiceResult
    if http_code == 200:
        pred = resp.json()
        pred_invoice = pred

    # 202 / 504 -- poll
    elif http_code in (202, 504):
        body = resp.json()
        job_id = body.get("job_id")
        if not job_id:
            return {"orig_file": url, "type": record.get("type", ""),
                    "status": "no_job_id", "latency_s": time.perf_counter() - t0,
                    "mismatches": []}
        deadline = time.perf_counter() + POLL_MAX_WAIT
        pred_invoice = None
        while time.perf_counter() < deadline:
            time.sleep(POLL_INTERVAL)
            try:
                poll = client.get(f"{api_base}/v1/receipts/{job_id}", timeout=15.0)
            except Exception:
                continue
            pcode = poll.status_code
            if pcode == 200:
                pred_invoice = poll.json()
                break
            elif pcode in (422, 503):
                return {"orig_file": url, "type": record.get("type", ""),
                        "status": "pipeline_failed",
                        "error_code": poll.json().get("error_code", ""),
                        "latency_s": time.perf_counter() - t0, "mismatches": []}
            # 202 -> still pending, keep polling
        if pred_invoice is None:
            return {"orig_file": url, "type": record.get("type", ""),
                    "status": "poll_timeout", "latency_s": time.perf_counter() - t0,
                    "mismatches": []}

    elif http_code == 429:
        return {"orig_file": url, "type": record.get("type", ""),
                "status": "backpressure_429", "latency_s": time.perf_counter() - t0,
                "mismatches": []}
    else:
        return {"orig_file": url, "type": record.get("type", ""),
                "status": f"http_{http_code}", "latency_s": time.perf_counter() - t0,
                "mismatches": []}

    latency = time.perf_counter() - t0

    # Compare
    gt = _normalize_label_record(record)
    _, mismatches = evaluate_single(pred_invoice, gt)

    return {
        "orig_file":  url,
        "type":       record.get("type", ""),
        "status":     "ok",
        "latency_s":  round(latency, 2),
        "mismatches": mismatches,
    }


# ─── Main ────────────────────────────────────────────────────────────────────

def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input",     required=True,
                    help="Split JSON file (dev_set.json or test_set.json)")
    ap.add_argument("--api",       default="http://localhost:8000")
    ap.add_argument("--psv",       default=os.environ.get("PROMPT_SEMANTIC_VERSION", "v3.5"),
                    help="Prompt semantic version label (used in report filename)")
    ap.add_argument("--split",     default=None,
                    help="Split label for report (auto-detected from filename if omitted)")
    ap.add_argument("--workers",   type=int, default=4,
                    help="Concurrent HTTP workers (default 4)")
    ap.add_argument("--sample",    type=int, default=0,
                    help="Evaluate only N random records (0 = all). Alias of --max-records.")
    ap.add_argument("--max-records", type=int, default=0,
                    help="CI-friendly alias for --sample. If both set, --max-records wins.")
    ap.add_argument("--seed",      type=int, default=42)
    ap.add_argument("--cache-dir", default="data/eval_images")
    ap.add_argument("--out",       default="eval_reports/",
                    help="Directory for output report JSON")
    args = ap.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

    src = Path(args.input)
    if not src.exists():
        log.error("Input not found: %s", src)
        return 1

    records: list[dict] = json.loads(src.read_text(encoding="utf-8"))

    # Auto-detect split name from filename
    split_label = args.split or (
        "dev"  if "dev"  in src.stem else
        "test" if "test" in src.stem else src.stem
    )

    # --max-records takes precedence over --sample if both supplied
    n_cap = args.max_records if args.max_records > 0 else args.sample
    if n_cap > 0 and n_cap < len(records):
        random.seed(args.seed)
        records = random.sample(records, n_cap)
        log.info("Sampled %d / %d records", n_cap, len(records))

    cache_dir = Path(args.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    log.info("Evaluating %d records | api=%s | workers=%d | psv=%s | split=%s",
             len(records), args.api, args.workers, args.psv, split_label)

    per_record_results: list[dict] = [None] * len(records)  # type: ignore[list-item]

    with httpx.Client(timeout=120.0) as client:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(_eval_one, rec, args.api, cache_dir, client): i
                for i, rec in enumerate(records)
            }
            for fut in as_completed(futures):
                i = futures[fut]
                try:
                    per_record_results[i] = fut.result()
                except Exception as exc:
                    per_record_results[i] = {
                        "orig_file": records[i].get("file", ""),
                        "type":      records[i].get("type", ""),
                        "status":    "exception",
                        "error":     str(exc),
                        "latency_s": 0.0,
                        "mismatches": [],
                    }
                done = sum(1 for r in per_record_results if r is not None)
                if done % 50 == 0 or done == len(records):
                    log.info("[%d/%d]", done, len(records))

    # ── Aggregate ──────────────────────────────────────────────────────────
    from collections import defaultdict

    n_success = sum(1 for r in per_record_results if r["status"] == "ok")
    n_failed  = len(per_record_results) - n_success
    latencies = [r["latency_s"] for r in per_record_results if r["status"] == "ok"]
    avg_lat   = sum(latencies) / len(latencies) if latencies else 0.0

    # Field accuracy (global)
    field_counts: dict[str, dict] = defaultdict(lambda: {"correct": 0, "total": 0})
    # Per-type field accuracy
    type_field_counts: dict[str, dict[str, dict]] = defaultdict(
        lambda: defaultdict(lambda: {"correct": 0, "total": 0})
    )
    type_n: dict[str, int] = defaultdict(int)

    for rec_result, record in zip(per_record_results, records):
        if rec_result["status"] != "ok":
            continue
        store_type = record.get("type", "unknown")
        type_n[store_type] += 1

        gt = _normalize_label_record(record)
        # Re-run evaluate_single to get per-field bool results
        # Reconstruct pred from mismatches (fields not in mismatches = correct = copy from GT)
        field_results, _ = evaluate_single(
            _build_pred_skeleton(rec_result["mismatches"], gt),
            gt,
        )
        mismatch_fields = {m["field"] for m in rec_result["mismatches"]}

        for field_name, bool_list in field_results.items():
            for is_correct in bool_list:
                field_counts[field_name]["total"] += 1
                if is_correct:
                    field_counts[field_name]["correct"] += 1
                type_field_counts[store_type][field_name]["total"] += 1
                if is_correct:
                    type_field_counts[store_type][field_name]["correct"] += 1

    def _pct(d: dict) -> float:
        return round(d["correct"] / d["total"] * 100, 1) if d["total"] else 0.0

    field_accuracy = {
        k: {"correct": v["correct"], "total": v["total"], "pct": _pct(v)}
        for k, v in sorted(field_counts.items())
    }
    by_type = {
        t: {
            "n": type_n[t],
            "field_accuracy": {
                k: {"correct": v["correct"], "total": v["total"], "pct": _pct(v)}
                for k, v in sorted(type_field_counts[t].items())
            }
        }
        for t in sorted(type_n)
    }

    overall_avg = (
        sum(v["pct"] for v in field_accuracy.values()) / len(field_accuracy)
        if field_accuracy else 0.0
    )

    report = {
        "psv":             args.psv,
        "split":           split_label,
        "input":           str(src),
        "api":             args.api,
        "git_sha":         os.environ.get("GITHUB_SHA")
                           or os.environ.get("GIT_SHA", "")
                           or "",
        "timestamp":       datetime.now().isoformat(timespec="seconds"),
        "n_total":         len(records),
        "n_success":       n_success,
        "n_failed":        n_failed,
        "avg_latency_s":   round(avg_lat, 2),
        "overall_avg_pct": round(overall_avg, 1),
        "field_accuracy":  field_accuracy,
        "by_type":         by_type,
        "per_record":      per_record_results,
    }

    # ── Print summary ──────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"EVAL REPORT  psv={args.psv}  split={split_label}  n={len(records)}")
    print(f"{'='*60}")
    print(f"Success: {n_success}  Failed: {n_failed}  Avg latency: {avg_lat:.2f}s")
    print(f"Overall avg accuracy: {overall_avg:.1f}%\n")
    print(f"{'Field':25s}  {'Correct':>8}  {'Total':>7}  {'%':>6}")
    print("-" * 52)
    for fname, fa in field_accuracy.items():
        print(f"  {fname:23s}  {fa['correct']:8d}  {fa['total']:7d}  {fa['pct']:5.1f}%")
    print()
    print("By store type:")
    for t, info in by_type.items():
        tm = info["field_accuracy"].get("total_money", {})
        nm = info["field_accuracy"].get("name", {})
        pn = info["field_accuracy"].get("product_name", {})
        print(f"  {t:15s}: n={info['n']:4d}  "
              f"name={nm.get('pct',0):5.1f}%  "
              f"total_money={tm.get('pct',0):5.1f}%  "
              f"product_name={pn.get('pct',0):5.1f}%")

    # ── Write report ───────────────────────────────────────────────────────
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"eval_report_{args.psv}_{split_label}_{ts}.json"
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nReport saved -> {out_path}")
    return 0


def _build_pred_skeleton(mismatches: list[dict], gt: dict) -> dict:
    """Build a minimal pred dict that reproduces the mismatch pattern.

    For fields NOT in mismatches, copy from GT (they were correct).
    For fields IN mismatches, use the predicted value from the mismatch record.
    This lets us re-run evaluate_single to get the full field_results dict.
    """
    import copy, re
    pred = copy.deepcopy(gt)
    for m in mismatches:
        field = m["field"]
        predicted = m["predicted"]
        # Product field?  e.g. "product[2].product_name"
        pm = re.match(r"product\[(\d+)\]\.(.+)", field)
        if pm:
            idx, pf = int(pm.group(1)), pm.group(2)
            prods = pred.setdefault("products", [])
            while len(prods) <= idx:
                prods.append({})
            prods[idx][pf] = predicted
        else:
            pred[field] = predicted
    return pred


if __name__ == "__main__":
    raise SystemExit(main())
