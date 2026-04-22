"""Label Verification Pipeline — Phase 1 + 2 + 3 + 4.

Architecture
────────────
Phase 1  Arithmetic pre-filter (free, no API)
         Compute Σ(product_total_money) ± discounts and compare with total_money.
         Store-aware sign handling:
           • Lotte: discount stored as POSITIVE string, formula = prod_sum − disc = total
           • Others: total_discount_money stored with sign, formula = prod_sum + disc = total
         Flags records where |computed − label| > ARITH_TOLERANCE (default 500 VND).

Phase 2  Cross-model re-extraction sweep (concurrent, rate-limited)
         For every gold record, send image + GT JSON to Gemini.
         Ask: "is the image the right receipt? verify/correct every field."
         Saves raw model responses incrementally to a JSONL checkpoint file so
         the run is resumable after interruption.

Phase 3  Disagreement triage (pure Python, no API)
         For each record that has a model response:
           a) image_mismatch → status = EXCLUDED
           b) model corrects a field AND arithmetic now passes → status = CORRECTED,
              apply correction to output
           c) model and GT disagree but arithmetic is ambiguous → status = FLAGGED
           d) model agrees with GT (or only low-confidence diffs) → status = OK

Phase 4  Output
         label_verified.json   — full 1443-record dataset with _verify_status tags
         verify_report.jsonl   — per-record detail log (corrections, diffs, reasons)
         human_review.csv      — records that need a human eye

Usage
─────
    # Full run (resumes if checkpoint exists)
    python scripts/verify_gt_labels.py

    # Dry-run phase 1 only (arithmetic report, no API)
    python scripts/verify_gt_labels.py --phase1-only

    # Re-run phase 3+4 from existing checkpoint (no new API calls)
    python scripts/verify_gt_labels.py --triage-only

    # Limit to first N records (testing)
    python scripts/verify_gt_labels.py --n 20

Environment
───────────
    TROLL_API_KEY   required
    TROLL_BASE_URL  optional (default https://chat.trollllm.xyz/v1)
    TROLL_MODEL     optional (default gemini-3.1-pro-preview)
"""
from __future__ import annotations

import argparse
import base64
import copy
import csv
import json
import logging
import os
import re
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.request import Request, urlopen

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ─── Constants ───────────────────────────────────────────────────────────────

UNCORRECTABLE_IDX: set[int] = {161, 589, 699, 765, 778, 1183}
ARITH_TOLERANCE = 500          # VND gap accepted as rounding
TOP_FIELDS = ["name", "type", "date", "time", "pos_id",
              "receipt_number", "cashier", "total_money", "barcode"]
PROD_FIELDS = ["product_name", "product_id", "product_unit_price",
               "product_quantity", "product_discount_money", "product_total_money"]

STATUS_OK         = "verified_ok"
STATUS_CORRECTED  = "corrected"
STATUS_FLAGGED    = "flagged_human"
STATUS_EXCLUDED   = "excluded"
STATUS_API_FAILED = "api_failed"


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _norm(v: object) -> str:
    return unicodedata.normalize("NFC", str(v or "").strip())


def _digits(v: object) -> int | None:
    """Strip separators and return int, or None if not numeric."""
    s = re.sub(r"[,.\s]", "", str(v or ""))
    return int(s) if s.lstrip("-").isdigit() else None


def _is_missing_critical(inv: dict) -> bool:
    for f in ("date", "receipt_number", "total_money"):
        if not str(inv.get(f) or "").strip():
            return True
    return False


# ─── Phase 1: Arithmetic check ───────────────────────────────────────────────

def arithmetic_check(inv: dict) -> dict:
    """
    Returns {ok: bool, computed: int|None, label: int|None, diff: int, note: str}
    """
    total_label = _digits(inv.get("total_money"))
    if total_label is None:
        return dict(ok=True, computed=None, label=None, diff=0,
                    note="non-numeric total_money — skip")

    prod_sum = 0
    products = inv.get("products") or []
    if not isinstance(products, list):
        return dict(ok=True, computed=None, label=total_label, diff=0,
                    note="products is not a list — skip")
    for p in products:
        if not isinstance(p, dict):
            return dict(ok=True, computed=None, label=total_label, diff=0,
                        note="product entry is not a dict — skip")
        ptot = _digits(p.get("product_total_money"))
        if ptot is None:
            return dict(ok=True, computed=None, label=total_label, diff=0,
                        note="non-numeric product_total_money — skip")
        prod_sum += ptot

    store_type = inv.get("type", "")
    disc_raw = str(inv.get("total_discount_money") or "").strip()

    if store_type == "lotte":
        # Lotte stores discount as positive string e.g. "89,200"
        disc_abs = _digits(disc_raw) if disc_raw else 0
        if disc_abs is None:
            disc_abs = 0
        computed = prod_sum - abs(disc_abs)
    else:
        # Others store with sign e.g. "-24,000" or ""
        disc_signed = _digits(disc_raw) if disc_raw else 0
        if disc_signed is None:
            disc_signed = 0
        computed = prod_sum + disc_signed

    diff = abs(computed - total_label)
    ok = diff <= ARITH_TOLERANCE
    return dict(ok=ok, computed=computed, label=total_label, diff=diff,
                note="" if ok else f"computed={computed:,} label={total_label:,} diff={diff:,}")


# ─── Phase 2: Model verification prompt ──────────────────────────────────────

_VERIFY_SYSTEM = """\
You are a Vietnamese receipt quality-verification assistant.

You will be given:
  1. A receipt image
  2. A JSON "draft label" that was extracted from this receipt (possibly with errors)

Your job is to verify every field in the draft label against the image and report:
  • Which fields are CORRECT (model agrees with label)
  • Which fields are WRONG (model sees a different value in the image)
  • Whether the image actually matches the receipt described in the label
    (i.e., receipt number, store name, and date in the label match what is in the image)

OUTPUT FORMAT — return a JSON object with EXACTLY these keys:

{
  "image_matches_label": true | false,
  "image_mismatch_reason": "string or empty",
  "corrections": [
    {
      "field": "field path e.g. 'name' or 'products[2].product_name'",
      "label_value": "what the label says",
      "correct_value": "what the image actually shows",
      "confidence": "high | medium | low",
      "reason": "one-line explanation"
    }
  ],
  "confirmed_ok": ["list of field names/paths that are correct as-is"],
  "unverifiable": ["list of field names that cannot be read from the image"],
  "notes": "any additional observations"
}

RULES:
1. image_matches_label = false ONLY if receipt_number OR store name in the image
   clearly differs from the label. Small OCR differences in branch name are NOT mismatches.
2. Only include a field in corrections if you are at least medium-confident it is wrong.
3. Vietnamese text: copy EXACTLY as printed (diacritics, capitalization, punctuation).
4. For products: use path format "products[N].field_name" (0-indexed).
5. total_money: digits only, no separators.
6. product_quantity: copy verbatim including commas, dots, and unit suffixes.
7. Output pure JSON only. No markdown fences.
"""


def _build_verify_prompt(inv: dict, arith: dict) -> str:
    # Produce a compact, readable version of the label for the prompt
    label_summary: dict = {}
    for f in TOP_FIELDS:
        v = inv.get(f)
        if v is not None and str(v).strip():
            label_summary[f] = v
    label_summary["type"] = inv.get("type", "")
    prods = []
    for i, p in enumerate(inv.get("products") or []):
        pd: dict = {}
        for pf in PROD_FIELDS:
            v = p.get(pf)
            if v is not None and str(v).strip():
                pd[pf] = v
        if pd:
            prods.append(pd)
    label_summary["products"] = prods
    # Lotte extras
    for extra in ("total_original_money", "total_discount_money"):
        if inv.get(extra):
            label_summary[extra] = inv[extra]

    lines = ["DRAFT LABEL (verify against the image):"]
    lines.append(json.dumps(label_summary, ensure_ascii=False, indent=2))
    if not arith["ok"] and arith["computed"] is not None:
        lines.append(
            f"\nNOTE: Arithmetic check FAILED — "
            f"sum of product totals = {arith['computed']:,} "
            f"but total_money label = {arith['label']:,} "
            f"(diff = {arith['diff']:,} VND). "
            f"Please verify total_money and all product_total_money values carefully."
        )
    lines.append("\nVerify the draft label against the receipt image and return the JSON verdict.")
    return "\n".join(lines)


# ─── Image download ───────────────────────────────────────────────────────────

def _download(url: str, retries: int = 3) -> bytes:
    for attempt in range(retries):
        try:
            req = Request(url, headers={"User-Agent": "verify/1.0"})
            with urlopen(req, timeout=30) as r:
                return r.read()
        except Exception as exc:
            if attempt == retries - 1:
                raise
            time.sleep(1.5 ** attempt)
    raise RuntimeError("unreachable")


# ─── Rate limiter ─────────────────────────────────────────────────────────────

class TokenBucket:
    def __init__(self, rpm: float) -> None:
        self._rate = rpm / 60.0
        self._tokens = rpm
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self._tokens + (now - self._last) * self._rate,
                    self._rate * 60,
                )
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self._rate
            time.sleep(wait)


# ─── API call ────────────────────────────────────────────────────────────────

def call_verify(client, model: str, image_bytes: bytes,
                inv: dict, arith: dict, retries: int = 6) -> dict:
    b64 = base64.b64encode(image_bytes).decode("ascii")
    user_text = _build_verify_prompt(inv, arith)

    for attempt in range(retries):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _VERIFY_SYSTEM},
                    {"role": "user", "content": [
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                        {"type": "text", "text": user_text},
                    ]},
                ],
                response_format={"type": "json_object"},
                temperature=0.0,
            )
            raw = response.choices[0].message.content or ""
            raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
            raw = re.sub(r"\s*```$", "", raw.strip())
            return json.loads(raw) if raw else {}
        except Exception as exc:
            # Retry on rate limit with exponential back-off
            is_429 = "429" in str(exc) or "rate_limit" in str(exc).lower()
            if is_429 and attempt < retries - 1:
                wait = 2.0 * (2 ** attempt)   # 2, 4, 8, 16, 32, 64 s
                log.warning("429 on attempt %d — sleeping %.1fs", attempt + 1, wait)
                time.sleep(wait)
                continue
            raise


# ─── Worker ──────────────────────────────────────────────────────────────────

def _process_one(
    orig_idx: int,
    inv: dict,
    arith: dict,
    client,
    model: str,
    bucket: TokenBucket,
    print_lock: threading.Lock,
) -> dict:
    url = inv.get("file", "")
    if not url:
        return dict(orig_idx=orig_idx, status="no_url", model_response=None,
                    arith=arith, error="")

    try:
        img = _download(url)
    except Exception as exc:
        return dict(orig_idx=orig_idx, status="download_error", model_response=None,
                    arith=arith, error=str(exc))

    bucket.acquire()

    try:
        resp = call_verify(client, model, img, inv, arith)
    except Exception as exc:
        return dict(orig_idx=orig_idx, status="api_error", model_response=None,
                    arith=arith, error=str(exc))

    return dict(orig_idx=orig_idx, status="ok", model_response=resp,
                arith=arith, error="")


# ─── Phase 3: Triage ─────────────────────────────────────────────────────────

def _apply_correction(inv: dict, field: str, new_value: str) -> None:
    """Mutate inv in-place applying a correction at field path."""
    m = re.match(r"products\[(\d+)\]\.(.+)", field)
    if m:
        idx, pf = int(m.group(1)), m.group(2)
        prods = inv.get("products") or []
        if 0 <= idx < len(prods):
            prods[idx][pf] = new_value
    elif field in inv:
        inv[field] = new_value


def triage(orig_idx: int, inv: dict, arith: dict,
           model_response: dict) -> dict:
    """
    Returns {
        status: STATUS_*,
        corrections_applied: [...],
        flagged_fields: [...],
        excluded_reason: str,
    }
    """
    result = dict(
        status=STATUS_OK,
        corrections_applied=[],
        flagged_fields=[],
        excluded_reason="",
    )

    # ── Image mismatch? ──
    if not model_response.get("image_matches_label", True):
        result["status"] = STATUS_EXCLUDED
        result["excluded_reason"] = model_response.get("image_mismatch_reason", "")
        return result

    corrections = model_response.get("corrections") or []
    if not corrections:
        # Model agrees with everything
        result["status"] = STATUS_OK
        return result

    CONF_RANK = {"high": 3, "medium": 2, "low": 1}
    accepted: list[dict] = []
    flagged: list[dict] = []

    for corr in corrections:
        if not isinstance(corr, dict):
            continue
        field = corr.get("field", "")
        if not field:
            continue  # malformed correction entry — skip
        correct_val = str(corr.get("correct_value") or "").strip()
        label_val   = str(corr.get("label_value") or "").strip()
        confidence  = corr.get("confidence", "low")
        reason      = corr.get("reason", "")

        if _norm(correct_val) == _norm(label_val):
            continue  # spurious diff

        rank = CONF_RANK.get(confidence, 0)

        # High/medium confidence corrections: accept
        if rank >= 2:
            accepted.append(corr)
        else:
            # Low confidence: flag for human
            flagged.append(corr)

    # Apply accepted corrections
    if accepted:
        for corr in accepted:
            _apply_correction(inv, corr["field"], str(corr.get("correct_value") or ""))
            result["corrections_applied"].append({
                "field": corr["field"],
                "from": corr.get("label_value"),
                "to":   corr.get("correct_value"),
                "confidence": corr.get("confidence"),
                "reason": corr.get("reason"),
            })
        result["status"] = STATUS_CORRECTED

    # Re-run arithmetic after corrections
    if accepted:
        arith_post = arithmetic_check(inv)
    else:
        arith_post = arith

    # Flagged fields
    if flagged:
        result["flagged_fields"] = [
            {"field": c["field"], "label": c.get("label_value"),
             "model": c.get("correct_value"), "reason": c.get("reason")}
            for c in flagged
        ]
        if result["status"] == STATUS_OK:
            result["status"] = STATUS_FLAGGED

    # If arithmetic still fails after corrections, flag regardless
    if not arith_post["ok"] and arith_post["computed"] is not None:
        if result["status"] not in (STATUS_EXCLUDED,):
            result["status"] = STATUS_FLAGGED
            result["flagged_fields"].append({
                "field": "arithmetic",
                "label": str(arith_post["label"]),
                "model": str(arith_post["computed"]),
                "reason": arith_post["note"],
            })

    return result


# ─── Checkpoint helpers ───────────────────────────────────────────────────────

def load_checkpoint(path: Path) -> dict[int, dict]:
    """Load existing checkpoint into {orig_idx: result_dict}."""
    done: dict[int, dict] = {}
    if not path.exists():
        return done
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
            done[r["orig_idx"]] = r
        except Exception:
            pass
    return done


def append_checkpoint(path: Path, lock: threading.Lock, record: dict) -> None:
    with lock:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ─── Main ────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input",       default="label_corrected.json")
    ap.add_argument("--output",      default="label_verified.json")
    ap.add_argument("--report",      default="verify_report.jsonl")
    ap.add_argument("--checkpoint",  default="verify_checkpoint.jsonl",
                    help="Incremental save — run is resumable if interrupted")
    ap.add_argument("--human-csv",   default="human_review.csv")
    ap.add_argument("--n",           type=int, default=0,
                    help="Process only first N gold records (0 = all)")
    ap.add_argument("--workers",     type=int, default=10)
    ap.add_argument("--rpm",         type=float, default=27.0,
                    help="API rate limit (default 27, headroom under 30)")
    ap.add_argument("--model",
                    default=os.environ.get("TROLL_MODEL", "gemini-3.1-pro-preview"))
    ap.add_argument("--phase1-only", action="store_true",
                    help="Run arithmetic check only — no API calls")
    ap.add_argument("--triage-only", action="store_true",
                    help="Skip API calls, triage from existing checkpoint")
    args = ap.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

    # ── Load data ──
    src_path = Path(args.input)
    all_records: list[dict] = json.loads(src_path.read_text(encoding="utf-8"))
    log.info("Loaded %d records from %s", len(all_records), src_path)

    # ── Build gold set ──
    gold: list[tuple[int, dict]] = [
        (i, inv) for i, inv in enumerate(all_records)
        if i not in UNCORRECTABLE_IDX and not _is_missing_critical(inv)
    ]
    if args.n > 0:
        gold = gold[: args.n]
    log.info("Gold set: %d records", len(gold))

    # ── Phase 1: Arithmetic ──
    arith_results: dict[int, dict] = {}
    arith_fail = 0
    for orig_idx, inv in gold:
        a = arithmetic_check(inv)
        arith_results[orig_idx] = a
        if not a["ok"]:
            arith_fail += 1

    log.info("Phase 1 arithmetic: %d/%d records fail (|diff| > %d VND)",
             arith_fail, len(gold), ARITH_TOLERANCE)

    if args.phase1_only:
        print(f"\nArithmetic failures: {arith_fail}/{len(gold)}")
        fail_records = [(i, inv) for i, inv in gold if not arith_results[i]["ok"]]
        for orig_idx, inv in fail_records[:20]:
            a = arith_results[orig_idx]
            print(f"  idx={orig_idx} [{inv.get('type')}] {a['note']}")
        return 0

    # ── Phase 2: API sweep ──
    ckpt_path = Path(args.checkpoint)
    ckpt_lock = threading.Lock()
    print_lock = threading.Lock()

    done_map = load_checkpoint(ckpt_path)
    log.info("Checkpoint: %d records already processed", len(done_map))

    remaining = [(i, inv) for i, inv in gold if i not in done_map]
    log.info("Remaining to process: %d", len(remaining))

    if not args.triage_only and remaining:
        api_key = os.environ.get("TROLL_API_KEY", "")
        if not api_key:
            log.error("TROLL_API_KEY not set"); return 1
        base_url = os.environ.get("TROLL_BASE_URL", "https://chat.trollllm.xyz/v1")
        try:
            from openai import OpenAI
        except ImportError:
            log.error("pip install openai"); return 1
        client = OpenAI(api_key=api_key, base_url=base_url)
        bucket = TokenBucket(rpm=args.rpm)

        log.info("Starting API sweep: %d workers, %.0f RPM, model=%s",
                 args.workers, args.rpm, args.model)

        futures = {}
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for orig_idx, inv in remaining:
                fut = pool.submit(
                    _process_one,
                    orig_idx, inv, arith_results[orig_idx],
                    client, args.model, bucket, print_lock,
                )
                futures[fut] = (orig_idx, inv)

            completed = 0
            api_errors = 0
            for fut in as_completed(futures):
                orig_idx, inv = futures[fut]
                completed += 1
                try:
                    res = fut.result()
                except Exception as exc:
                    res = dict(orig_idx=orig_idx, status="api_error",
                               model_response=None, arith=arith_results[orig_idx],
                               error=str(exc))

                append_checkpoint(ckpt_path, ckpt_lock, res)
                done_map[orig_idx] = res

                if res["status"] == "api_error":
                    api_errors += 1
                mismatch = (res.get("model_response") or {}).get("image_matches_label")
                with print_lock:
                    log.info("[%d/%d] idx=%-5d type=%-10s status=%-15s %s",
                             completed, len(remaining), orig_idx,
                             inv.get("type", "?"), res["status"],
                             "IMAGE_MISMATCH" if mismatch is False else "")

        log.info("API sweep complete. errors=%d", api_errors)

    # ── Phase 3: Triage ──
    log.info("Phase 3: triaging %d processed records …", len(done_map))

    # Work on deep copies so we can apply corrections safely
    output_records = [copy.deepcopy(r) for r in all_records]
    report_entries: list[dict] = []
    status_counts: dict[str, int] = {
        STATUS_OK: 0, STATUS_CORRECTED: 0,
        STATUS_FLAGGED: 0, STATUS_EXCLUDED: 0,
        STATUS_API_FAILED: 0, "skipped": 0,
    }

    human_rows: list[dict] = []

    for orig_idx, inv_orig in gold:
        rec = done_map.get(orig_idx)
        if rec is None:
            output_records[orig_idx]["_verify_status"] = "skipped"
            status_counts["skipped"] += 1
            continue

        if rec["status"] in ("api_error", "download_error", "no_url"):
            output_records[orig_idx]["_verify_status"] = STATUS_API_FAILED
            status_counts[STATUS_API_FAILED] += 1
            report_entries.append({
                "orig_idx": orig_idx,
                "type": inv_orig.get("type"),
                "status": STATUS_API_FAILED,
                "error": rec.get("error", ""),
            })
            continue

        model_resp = rec.get("model_response") or {}
        arith = rec.get("arith") or arith_results.get(orig_idx, {})

        tri = triage(orig_idx, output_records[orig_idx], arith, model_resp)

        output_records[orig_idx]["_verify_status"] = tri["status"]
        status_counts[tri["status"]] = status_counts.get(tri["status"], 0) + 1

        report_entry: dict = {
            "orig_idx": orig_idx,
            "type": inv_orig.get("type"),
            "receipt_number": inv_orig.get("receipt_number"),
            "status": tri["status"],
            "arith_ok": arith.get("ok", True),
            "arith_note": arith.get("note", ""),
            "corrections_applied": tri["corrections_applied"],
            "flagged_fields": tri["flagged_fields"],
            "excluded_reason": tri["excluded_reason"],
            "model_notes": model_resp.get("notes", ""),
        }
        report_entries.append(report_entry)

        if tri["status"] == STATUS_FLAGGED:
            for ff in tri["flagged_fields"]:
                human_rows.append({
                    "orig_idx": orig_idx,
                    "type": inv_orig.get("type"),
                    "receipt_number": inv_orig.get("receipt_number"),
                    "file_url": inv_orig.get("file", ""),
                    "field": ff.get("field"),
                    "label_value": ff.get("label"),
                    "model_value": ff.get("model"),
                    "reason": ff.get("reason"),
                    "human_decision": "",  # blank for human to fill
                })
        elif tri["status"] == STATUS_EXCLUDED:
            human_rows.append({
                "orig_idx": orig_idx,
                "type": inv_orig.get("type"),
                "receipt_number": inv_orig.get("receipt_number"),
                "file_url": inv_orig.get("file", ""),
                "field": "ENTIRE_RECORD",
                "label_value": "",
                "model_value": "",
                "reason": tri["excluded_reason"],
                "human_decision": "",
            })

    # ── Phase 4: Output ──
    out_path = Path(args.output)
    out_path.write_text(
        json.dumps(output_records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    rpt_path = Path(args.report)
    rpt_path.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in report_entries),
        encoding="utf-8",
    )

    csv_path = Path(args.human_csv)
    if human_rows:
        with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "orig_idx", "type", "receipt_number", "file_url",
                "field", "label_value", "model_value", "reason", "human_decision",
            ])
            writer.writeheader()
            writer.writerows(human_rows)

    print("\n" + "=" * 60)
    print("VERIFICATION SUMMARY")
    print("=" * 60)
    total_gold = len(gold)
    for status, count in sorted(status_counts.items(), key=lambda x: -x[1]):
        pct = count / total_gold * 100
        print(f"  {status:20s}: {count:5d}  ({pct:.1f}%)")
    print("-" * 60)
    print(f"  {'TOTAL GOLD':20s}: {total_gold:5d}")
    usable = status_counts.get(STATUS_OK, 0) + status_counts.get(STATUS_CORRECTED, 0)
    print(f"  {'USABLE (ok+corrected)':20s}: {usable:5d}  ({usable/total_gold*100:.1f}%)")
    print()
    print(f"Verified labels  → {out_path}")
    print(f"Report           → {rpt_path}")
    print(f"Human review CSV → {csv_path}  ({len(human_rows)} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
