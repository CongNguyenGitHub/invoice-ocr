"""Ground-truth label auto-correction via Gemini + image.

Error patterns found in label_minitet_festive_24_v3_public.json:

  1. ### blocks (3.0% of records)
     — OCR confidence collapse; scanner substituted ### for unreadable text.
     — Occurs in product_name, barcode, receipt_number, time.
     — Correctable: image contains original text.

  2. ? replacing Vietnamese diacritic (1.6% of records)
     — POS system dropped the high-byte of a UTF-8 diacritic, left bare ?.
     — e.g. "TA?O HO?NG" → "TÁO HỒNG",  "B?NG" → "BĂNG"/"BÔNG"/"BẰNG".
     — Correctable: image shows correct Vietnamese glyph.

  3. + replacing first character (rare, ~5 records)
     — Same byte-drop issue; e.g. "+RONG BI?N" → "RONG BIỂN".
     — Correctable: image shows correct character.

  4. Truncated product names (31.9% of products, non-unit-ending)
     — POS field limit ~35-40 chars; receipt itself shows the same truncation.
     — Partially correctable: LLM may infer from packaging visible in image,
       but we only attempt this when the name ends mid-word (non-unit char).

NOT corrected (legitimate data):
  * multiplier/quantity format  e.g. "110ML*4" — keep as-is.
  @ category prefix             e.g. "@WASABI-AKURUHI990" — keep as-is.
  & ampersand in name           e.g. "DETOX GỪNG & TRÀ" — keep as-is.

Usage:
    # Dry-run: detect errors only, no API calls
    python scripts/correct_gt_labels.py --dry-run

    # Correct first 20 dirty records (concurrent)
    python scripts/correct_gt_labels.py --n 20

    # Full run (all dirty records, all types):
    python scripts/correct_gt_labels.py \\
        --input label_minitet_festive_24_v3_public.json \\
        --output label_corrected.json

    # Only aeon + emart, also attempt truncation fixes:
    python scripts/correct_gt_labels.py --types aeon emart --include-truncated

    # Tune concurrency to match RPM quota (default 10 workers for 30 RPM):
    python scripts/correct_gt_labels.py --workers 10

Environment:
    TROLL_API_KEY  — required  (OpenAI-compatible key for chat.trollllm.xyz)
    TROLL_BASE_URL — optional  (default: https://chat.trollllm.xyz/v1)
    TROLL_MODEL    — optional  (default: gemini-3.1-pro-preview)
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

# ─────────────────────────── Error detection ────────────────────────────────

_UNIT_ENDINGS = frozenset("GMLKCNTDXPQRHSAZBEF")  # common unit abbreviations
_HASH_RE = re.compile(r"#{2,}")
_QQQ_RE = re.compile(r"\?{3,}")
_MID_Q_RE = re.compile(r"\w\?\w")     # diacritic substitution: A?O, B?N …
_PLUS_START_RE = re.compile(
    r"^\+[A-ZÀÁÂÃÈÉÊÌÍÒÓÔÕÙÚĂĐƠƯẠẶẨẬẮẪẤẢẼẺẾỀỆỊỢỞỜỌỐỔỘỚỤỰỨỪỬỮ]",
    re.UNICODE,
)


def _all_text_fields(inv: dict) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for field in ["name", "date", "time", "pos_id", "receipt_number",
                  "cashier", "total_money", "barcode"]:
        pairs.append((field, str(inv.get(field) or "")))
    for i, p in enumerate(inv.get("products") or []):
        for pf in ["product_id", "product_name", "product_unit_price",
                   "product_quantity", "product_discount_money",
                   "product_total_money", "product_amount", "second_product_name"]:
            pairs.append((f"products[{i}].{pf}", str(p.get(pf) or "")))
    return pairs


def detect_errors(inv: dict) -> list[dict]:
    errors: list[dict] = []
    seen_paths: set[str] = set()

    for path, val in _all_text_fields(inv):
        m = re.match(r"products\[(\d+)\]\.(.+)", path)
        prod_idx = int(m.group(1)) if m else None
        field = m.group(2) if m else path

        if _HASH_RE.search(val) or _QQQ_RE.search(val):
            errors.append(dict(field=field, path=path, value=val,
                               kind="unreadable_block", product_idx=prod_idx))
            seen_paths.add(path)
        elif _MID_Q_RE.search(val):
            errors.append(dict(field=field, path=path, value=val,
                               kind="diacritic_substitution", product_idx=prod_idx))
            seen_paths.add(path)
        elif _PLUS_START_RE.match(val):
            errors.append(dict(field=field, path=path, value=val,
                               kind="plus_prefix", product_idx=prod_idx))
            seen_paths.add(path)

    for i, p in enumerate(inv.get("products") or []):
        name = str(p.get("product_name") or "")
        path = f"products[{i}].product_name"
        if (len(name) >= 35 and name
                and name[-1].upper() not in _UNIT_ENDINGS
                and path not in seen_paths):
            errors.append(dict(field="product_name", path=path, value=name,
                               kind="truncated_name", product_idx=i))
    return errors


# ─────────────────────────── Image download ──────────────────────────────────

def _download(url: str, retries: int = 3) -> bytes:
    for attempt in range(retries):
        try:
            req = Request(url, headers={"User-Agent": "gt-corrector/1.0"})
            with urlopen(req, timeout=30) as r:
                return r.read()
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(1.5 ** attempt)
    raise RuntimeError("unreachable")


# ─────────────────────────── Prompts & schema ───────────────────────────────

_SYSTEM_PROMPT = """\
You are a Vietnamese retail receipt quality-correction assistant.
You will be shown a receipt image and a list of known OCR errors extracted from
that receipt by a POS-scanner label tool. Your job is to correct ONLY those errors
by reading the original text directly from the receipt image.

=== ERROR KINDS ===
  unreadable_block      : "###" or "???" was substituted for unreadable text.
                          Find the actual text visible in the image.
  diacritic_substitution: A Vietnamese diacritic was garbled into "?".
                          The "?" is EXACTLY one missing character or tone mark.
                          e.g. "TA?O HO?NG" → "TÁO HỒNG"  |  "B?NG" → "BÔNG"
  plus_prefix           : First character garbled into "+".
                          e.g. "+RONG BI?N" → "RONG BIỂN"
  truncated_name        : Name cut off at ~35-40 chars by POS field limit.
                          Extend ONLY if full name is clearly visible in image.
                          Do NOT invent text that is not on the receipt.

=== STRICT RULES ===
1. Correct ONLY the fields listed under "FIELDS WITH ERRORS".
2. Vietnamese text must use full Unicode diacritics (UTF-8), not ASCII.
3. If a field is not clearly readable, put it in "uncorrectable". Do NOT guess.
4. Preserve all non-error characters exactly (case, spacing, punctuation).
5. Do NOT touch legitimate formats:
   - "110ML*4"  — asterisk is a quantity multiplier, keep as-is
   - "@WASABI"  — @ is an internal category prefix, keep as-is
   - "GỪNG & TRÀ" — & in product name is valid, keep as-is
6. Confidence: high = clearly visible, medium = very probable, low = guess.
   Prefer "uncorrectable" over low-confidence guesses.

=== OUTPUT FORMAT (strict) ===
Return a JSON object with EXACTLY these two top-level keys:
  "corrections"   : array of objects, each with EXACTLY these keys:
                      "path"            — field path e.g. "products[2].product_name"
                      "corrected_value" — the corrected string
                      "confidence"      — one of: "high" | "medium" | "low"
                      "reason"          — one-sentence explanation
  "uncorrectable" : array of objects, each with EXACTLY these keys:
                      "path"   — field path
                      "reason" — why it cannot be corrected

Both arrays may be empty. Output pure JSON only — no markdown, no code fences.
"""


def _build_user_prompt(inv: dict, errors: list[dict]) -> str:
    lines = [
        f"Store type : {inv.get('type', 'unknown')}",
        f"Store name : {inv.get('name', '')}",
        f"Receipt    : {inv.get('receipt_number', '')}",
        f"Date       : {inv.get('date', '')}",
        "",
        "=== FIELDS WITH ERRORS ===",
    ]
    for e in errors:
        lines.append(
            f"  kind={e['kind']}  path={e['path']}  current_value={e['value']!r}"
        )
    lines += [
        "",
        "Look at the receipt image and correct each field listed above.",
        "Return corrections and uncorrectable fields in the JSON format described.",
    ]
    return "\n".join(lines)


# ─────────────────────────── API call ────────────────────────────────────────

def call_api(
    client: Any,
    model: str,
    image_bytes: bytes,
    inv: dict,
    errors: list[dict],
) -> dict:
    b64 = base64.b64encode(image_bytes).decode("ascii")

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    {"type": "text", "text": _build_user_prompt(inv, errors)},
                ],
            },
        ],
        response_format={"type": "json_object"},
        temperature=0.0,
    )
    raw = response.choices[0].message.content or ""
    # Strip markdown fences if provider wraps output
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw.strip())
    return json.loads(raw) if raw else {}


# ─────────────────────────── Apply corrections ───────────────────────────────

def apply_corrections(inv: dict, corrections: list[dict]) -> dict:
    import copy
    result = copy.deepcopy(inv)
    for corr in corrections:
        path = corr["path"]
        value = corr["corrected_value"]
        m = re.match(r"products\[(\d+)\]\.(.+)", path)
        if m:
            idx, field = int(m.group(1)), m.group(2)
            if 0 <= idx < len(result.get("products") or []):
                result["products"][idx][field] = value
        elif path in result:
            result[path] = value
    return result


# ─────────────────────────── Rate limiter ────────────────────────────────────

class TokenBucket:
    """Simple token-bucket rate limiter (thread-safe)."""

    def __init__(self, rate_per_minute: float) -> None:
        self._rate = rate_per_minute / 60.0   # tokens per second
        self._tokens = rate_per_minute        # start full
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens += (now - self._last) * self._rate
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self._rate
            time.sleep(wait)


# ─────────────────────────── Worker function ─────────────────────────────────

def _process_one(
    orig_idx: int,
    inv: dict,
    errs: list[dict],
    client: Any,
    model: str,
    bucket: TokenBucket,
    min_rank: int,
    print_lock: threading.Lock,
) -> dict:
    """Download image, call API, return result dict. Called from thread pool."""
    result_entry = {
        "orig_idx": orig_idx,
        "inv": inv,
        "accepted": [],
        "skipped": [],
        "uncorrectable": [],
        "status": "ok",
        "raw_result": None,
    }

    url = inv.get("file", "")
    if not url:
        result_entry["status"] = "no_image_url"
        return result_entry

    try:
        image_bytes = _download(url)
    except Exception as exc:
        result_entry["status"] = f"download_error: {exc}"
        return result_entry

    # Acquire rate-limit token before API call
    bucket.acquire()

    try:
        raw = call_api(client, model, image_bytes, inv, errs)
    except Exception as exc:
        result_entry["status"] = f"gemini_error: {exc}"
        return result_entry

    result_entry["raw_result"] = raw
    confidence_rank = {"high": 3, "medium": 2, "low": 1}
    corrections = raw.get("corrections") or []
    uncorrectable = raw.get("uncorrectable") or []

    accepted = [c for c in corrections
                if confidence_rank.get(c.get("confidence", "low"), 0) >= min_rank]
    skipped = [c for c in corrections
               if confidence_rank.get(c.get("confidence", "low"), 0) < min_rank]

    result_entry["accepted"] = accepted
    result_entry["skipped"] = skipped
    result_entry["uncorrectable"] = uncorrectable
    return result_entry


# ─────────────────────────── Logging ─────────────────────────────────────────

def _log(report_path: Path, log_lock: threading.Lock,
         orig_idx: int, inv: dict, errors: list[dict],
         result: dict | None, status: str) -> None:
    entry = {
        "orig_idx": orig_idx,
        "type": inv.get("type"),
        "receipt_number": inv.get("receipt_number"),
        "file": inv.get("file", ""),
        "status": status,
        "errors": errors,
        "corrections": (result or {}).get("corrections"),
        "uncorrectable": (result or {}).get("uncorrectable"),
    }
    with log_lock:
        with report_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ─────────────────────────── Main ────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--input", default="label_minitet_festive_24_v3_public.json")
    ap.add_argument("--output", default="label_corrected.json")
    ap.add_argument("--report", default="correction_report.jsonl")
    ap.add_argument("--n", type=int, default=0,
                    help="Process only first N dirty records (0 = all)")
    ap.add_argument("--types", nargs="+",
                    help="Limit to store types  e.g. --types aeon emart")
    ap.add_argument("--error-kinds", nargs="+",
                    default=["unreadable_block", "diacritic_substitution", "plus_prefix"],
                    help="Error kinds to correct (default excludes truncated_name)")
    ap.add_argument("--include-truncated", action="store_true",
                    help="Also attempt truncated_name corrections")
    ap.add_argument("--min-confidence", default="medium",
                    choices=["high", "medium", "low"])
    ap.add_argument("--dry-run", action="store_true",
                    help="Detect errors only — no API calls")
    ap.add_argument("--model",
                    default=os.environ.get("TROLL_MODEL", "gemini-3.1-pro-preview"))
    ap.add_argument("--retry-failed", metavar="REPORT_JSONL",
                    help="Re-process only records that have gemini_error status in a previous report")
    ap.add_argument("--workers", type=int, default=10,
                    help="Concurrent API workers (default 10, fits 30 RPM with ~3s/call)")
    ap.add_argument("--rpm", type=float, default=28.0,
                    help="API rate limit in requests-per-minute (default 28, leaves headroom under 30)")
    args = ap.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

    # ── Load ──
    gt_path = Path(args.input)
    if not gt_path.exists():
        print(f"ERROR: {gt_path} not found", file=sys.stderr)
        return 1
    all_records: list[dict] = json.loads(gt_path.read_text(encoding="utf-8"))
    print(f"Loaded {len(all_records)} records from {gt_path}")

    work_records = all_records
    if args.types:
        work_records = [r for r in all_records if r.get("type") in args.types]
        print(f"After type filter {args.types}: {len(work_records)} records")

    # ── Detect ──
    target_kinds = set(args.error_kinds)
    if args.include_truncated:
        target_kinds.add("truncated_name")

    # Build dirty list, tracking ORIGINAL index in all_records
    {r["_orig"]: r for r in work_records} if False else {}
    # We need orig index in all_records, not work_records
    dirty: list[tuple[int, dict, list[dict]]] = []
    for orig_idx, inv in enumerate(all_records):
        if args.types and inv.get("type") not in args.types:
            continue
        relevant = [e for e in detect_errors(inv) if e["kind"] in target_kinds]
        if relevant:
            dirty.append((orig_idx, inv, relevant))

    # If --retry-failed, narrow to only previously failed indices
    if args.retry_failed:
        retry_path = Path(args.retry_failed)
        if not retry_path.exists():
            print(f"ERROR: retry report {retry_path} not found", file=sys.stderr)
            return 1
        failed_idxs = set()
        for line in retry_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            if entry.get("status", "").startswith("gemini_error"):
                failed_idxs.add(entry["orig_idx"])
        dirty = [(i, inv, errs) for i, inv, errs in dirty if i in failed_idxs]
        print(f"Retry mode: {len(dirty)} previously-failed records")

    kind_counts: dict[str, int] = {}
    for _, _, errs in dirty:
        for e in errs:
            kind_counts[e["kind"]] = kind_counts.get(e["kind"], 0) + 1

    print("\n=== Error scan ===")
    print(f"Dirty records : {len(dirty)} / {len(all_records)} "
          f"({100*len(dirty)/max(len(all_records),1):.1f}%)")
    for kind, count in sorted(kind_counts.items(), key=lambda x: -x[1]):
        print(f"  {kind:30s}: {count}")

    if args.dry_run:
        print("\n[DRY RUN] Sample dirty records:")
        for orig_idx, inv, errs in dirty[:10]:
            url_short = (inv.get("file") or "")[-50:]
            print(f"\n  idx={orig_idx}  type={inv.get('type')}  "
                  f"receipt={inv.get('receipt_number')}  url=...{url_short}")
            for e in errs:
                print(f"    [{e['kind']}] {e['path']} = {e['value']!r}")
        return 0

    # ── Setup ──
    api_key = os.environ.get("TROLL_API_KEY", "")
    if not api_key:
        print("ERROR: TROLL_API_KEY not set", file=sys.stderr)
        return 1
    base_url = os.environ.get("TROLL_BASE_URL", "https://chat.trollllm.xyz/v1")
    try:
        from openai import OpenAI
    except ImportError:
        print("ERROR: openai package not installed — run: pip install openai",
              file=sys.stderr)
        return 1
    client = OpenAI(api_key=api_key, base_url=base_url)

    confidence_rank = {"high": 3, "medium": 2, "low": 1}
    min_rank = confidence_rank[args.min_confidence]

    bucket = TokenBucket(rate_per_minute=args.rpm)
    print_lock = threading.Lock()
    log_lock = threading.Lock()

    corrected_records = list(all_records)  # will mutate in-place

    # When retrying, seed corrected_records from the previous output if it exists
    out_path = Path(args.output)
    if args.retry_failed and out_path.exists():
        prev = json.loads(out_path.read_text(encoding="utf-8"))
        if len(prev) == len(all_records):
            corrected_records = prev
            print(f"Seeded corrected_records from existing {out_path}")

    report_path = Path(args.report)
    # Append to existing report on retry, truncate on fresh run
    if not args.retry_failed:
        report_path.write_text("", encoding="utf-8")

    to_process = dirty[: args.n] if args.n > 0 else dirty
    stats = {k: 0 for k in ["attempted", "corrected", "skipped_confidence",
                              "uncorrectable", "download_failed", "api_error", "no_change"]}

    print(f"\nProcessing {len(to_process)} records  "
          f"(workers={args.workers}  rpm={args.rpm}  "
          f"model={args.model}  min_confidence={args.min_confidence})\n")

    # Submit all to thread pool, preserve ordering for output
    futures = {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for orig_idx, inv, errs in to_process:
            fut = pool.submit(
                _process_one,
                orig_idx, inv, errs, client, args.model,
                bucket, min_rank, print_lock,
            )
            futures[fut] = (orig_idx, inv, errs)

        completed = 0
        for fut in as_completed(futures):
            orig_idx, inv, errs = futures[fut]
            completed += 1
            try:
                res = fut.result()
            except Exception as exc:
                with print_lock:
                    print(f"  [FATAL] idx={orig_idx}: {exc}")
                stats["api_error"] += 1
                continue

            status = res["status"]
            accepted = res["accepted"]
            skipped = res["skipped"]
            uncorrectable = res["uncorrectable"]

            with print_lock:
                print(f"[{completed}/{len(to_process)}] idx={orig_idx}  "
                      f"type={inv.get('type')}  receipt={inv.get('receipt_number')}  "
                      f"errors={len(errs)}  status={status}")
                for e in errs:
                    print(f"  > [{e['kind']}] {e['path']} = {e['value']!r}")

            stats["attempted"] += 1

            if "download_error" in status or status == "no_image_url":
                stats["download_failed"] += 1
                _log(report_path, log_lock, orig_idx, inv, errs, None, status)
                continue
            if "gemini_error" in status:
                stats["api_error"] += 1
                _log(report_path, log_lock, orig_idx, inv, errs, None, status)
                continue

            if skipped:
                for c in skipped:
                    with print_lock:
                        print(f"  ~ [{c.get('confidence')}] SKIPPED {c['path']} → "
                              f"{c.get('corrected_value','')!r}  (below min_confidence)")
                stats["skipped_confidence"] += len(skipped)

            if accepted:
                for c in accepted:
                    with print_lock:
                        print(f"  ✓ [{c['confidence']}] {c['path']} → "
                              f"{c.get('corrected_value','')!r}  ({c.get('reason','')})")
                corrected_records[orig_idx] = apply_corrections(inv, accepted)
                stats["corrected"] += 1
            else:
                stats["no_change"] += 1

            for u in uncorrectable:
                with print_lock:
                    print(f"  ✗ uncorrectable: {u['path']} — {u.get('reason','')}")
                stats["uncorrectable"] += 1

            _log(report_path, log_lock, orig_idx, inv, errs, res["raw_result"], status)

    # ── Save ──
    out_path = Path(args.output)
    out_path.write_text(
        json.dumps(corrected_records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("\n=== SUMMARY ===")
    for k, v in stats.items():
        print(f"  {k:25s}: {v}")
    print(f"\nCorrected labels → {out_path}")
    print(f"Correction log   → {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
