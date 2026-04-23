"""
load_test.py -- Step-load traffic test with SLA assertions.

Tests the live API against a two-phase load profile that models the
10k to 20k requests/day ramp described in architecture_v3_final.md section 3.

Load profile
────────────
  Phase 1  warm_10k:   1.5 RPS  for  60 s  (~90 requests)
  Phase 2  spike_20k:  3.0 RPS  for 120 s  (~360 requests)
  Total ≈ 450 requests in ~3 minutes

Rate math:
  10k req/day in an 8-hour business window = 1,250 req/hr = 0.35 RPS sustained.
  Rush-hour burst headroom ≈ 4× = 1.4 RPS -> rounded up to 1.5 RPS.
  20k/day ≈ 3.0 RPS at the same burst ratio.

SLA thresholds (from arch section3 budget):
  p95 < 10 s  (total request latency incl. poll)
  p99 < 30 s
  error rate < 1 %   (4xx/5xx and network errors, NOT 429 backpressure)

Exit codes
──────────
  0  All SLA assertions passed
  1  One or more SLA assertions breached (CI-readable)

Usage
─────
    python scripts/load_test.py
    python scripts/load_test.py --api http://localhost:8000 \\
        --input data/eval/dev_set.json \\
        --report load_reports/

    # Custom phases (JSON list of {name, rps, duration_s})
    python scripts/load_test.py --phases '[{"name":"smoke","rps":0.5,"duration_s":30}]'
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx

# ─── Configuration ────────────────────────────────────────────────────────────

@dataclass
class Phase:
    name:       str
    rps:        float
    duration_s: float


DEFAULT_PHASES = [
    Phase("warm_10k",  rps=1.5, duration_s=60),
    Phase("spike_20k", rps=3.0, duration_s=120),
]

SLA = {
    "p95_s":           10.0,
    "p99_s":           30.0,
    "error_rate_pct":   1.0,
}

POLL_INTERVAL = 1.5   # s between polls
POLL_TIMEOUT  = 90.0  # s max poll per job


# ─── Request result ───────────────────────────────────────────────────────────

@dataclass
class RequestResult:
    phase:       str
    status:      str            # ok | error | backpressure | timeout
    http_code:   int  = 0
    submit_lat:  float = 0.0   # POST -> 200/202/504 latency
    total_lat:   float = 0.0   # full round-trip incl. polling
    error_type:  str  = ""


# ─── Image pool ───────────────────────────────────────────────────────────────

def _load_image_pool(input_path: str, cache_dir: Path, limit: int = 200) -> list[bytes]:
    """Pre-download up to `limit` images into memory for fast reuse during the test."""
    records = json.loads(Path(input_path).read_text(encoding="utf-8"))
    random.shuffle(records)
    pool: list[bytes] = []
    for rec in records:
        url = rec.get("file", "")
        if not url:
            continue
        fname = url.split("/")[-1]
        cached = cache_dir / fname
        if cached.exists():
            pool.append(cached.read_bytes())
        else:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "loadtest/1.0"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    data = r.read()
                cached.write_bytes(data)
                pool.append(data)
            except Exception:
                continue
        if len(pool) >= limit:
            break
    return pool


# ─── Single async request ─────────────────────────────────────────────────────

async def _do_request(
    client: httpx.AsyncClient,
    img_pool: list[bytes],
    api_base: str,
    phase_name: str,
) -> RequestResult:
    img = random.choice(img_pool)
    t0  = time.perf_counter()

    try:
        resp = await client.post(
            f"{api_base}/v1/receipts",
            files={"file": ("receipt.jpg", img, "image/jpeg")},
            timeout=30.0,
        )
        submit_lat = time.perf_counter() - t0
        code = resp.status_code
    except Exception as exc:
        return RequestResult(phase=phase_name, status="error",
                             total_lat=time.perf_counter() - t0,
                             error_type=f"network:{type(exc).__name__}")

    # 429 backpressure -- record separately, not counted as error
    if code == 429:
        return RequestResult(phase=phase_name, status="backpressure",
                             http_code=429, submit_lat=submit_lat,
                             total_lat=time.perf_counter() - t0,
                             error_type="backpressure_429")

    # 4xx/5xx that aren't 429
    if code >= 400:
        return RequestResult(phase=phase_name, status="error",
                             http_code=code, submit_lat=submit_lat,
                             total_lat=time.perf_counter() - t0,
                             error_type=f"http_{code}")

    # 200 -- synchronous success
    if code == 200:
        return RequestResult(phase=phase_name, status="ok",
                             http_code=200, submit_lat=submit_lat,
                             total_lat=time.perf_counter() - t0)

    # 202 / 504 -- poll for result
    job_id = (resp.json() or {}).get("job_id")
    if not job_id:
        return RequestResult(phase=phase_name, status="error",
                             http_code=code, submit_lat=submit_lat,
                             total_lat=time.perf_counter() - t0,
                             error_type="no_job_id")

    deadline = time.perf_counter() + POLL_TIMEOUT
    while time.perf_counter() < deadline:
        await asyncio.sleep(POLL_INTERVAL)
        try:
            poll = await client.get(f"{api_base}/v1/receipts/{job_id}", timeout=10.0)
        except Exception:
            continue
        pcode = poll.status_code
        if pcode == 200:
            return RequestResult(phase=phase_name, status="ok",
                                 http_code=200, submit_lat=submit_lat,
                                 total_lat=time.perf_counter() - t0)
        if pcode in (422, 503):
            return RequestResult(phase=phase_name, status="error",
                                 http_code=pcode, submit_lat=submit_lat,
                                 total_lat=time.perf_counter() - t0,
                                 error_type=f"pipeline_failed_{pcode}")
        # 202 -> still processing, keep polling

    return RequestResult(phase=phase_name, status="timeout",
                         http_code=code, submit_lat=submit_lat,
                         total_lat=time.perf_counter() - t0,
                         error_type="poll_timeout")


# ─── Phase runner ─────────────────────────────────────────────────────────────

async def _run_phase(
    phase: Phase,
    img_pool: list[bytes],
    api_base: str,
    results: list[RequestResult],
) -> None:
    """Fire requests at `phase.rps` for `phase.duration_s` seconds."""
    interval   = 1.0 / phase.rps
    start      = time.perf_counter()
    n_sent     = 0
    pending:   list[asyncio.Task] = []

    limits = httpx.Limits(max_connections=100, max_keepalive_connections=40)
    async with httpx.AsyncClient(limits=limits) as client:
        while time.perf_counter() - start < phase.duration_s:
            fire_at = start + n_sent * interval
            now     = time.perf_counter()
            wait    = fire_at - now
            if wait > 0:
                await asyncio.sleep(wait)

            task = asyncio.create_task(
                _do_request(client, img_pool, api_base, phase.name)
            )
            pending.append(task)
            n_sent += 1

        # Wait for all in-flight requests to complete (up to POLL_TIMEOUT extra)
        if pending:
            done, _ = await asyncio.wait(pending, timeout=POLL_TIMEOUT + 10)
            for t in done:
                results.append(t.result())
            for t in _:
                t.cancel()
                results.append(RequestResult(phase=phase.name, status="timeout",
                                             error_type="phase_drain_timeout"))


# ─── Statistics ───────────────────────────────────────────────────────────────

def _percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = math.ceil(p / 100.0 * len(sorted_vals)) - 1
    return sorted_vals[max(0, idx)]


def _phase_stats(results: list[RequestResult], phase_name: str) -> dict:
    phase_r = [r for r in results if r.phase == phase_name]
    if not phase_r:
        return {}
    ok        = [r for r in phase_r if r.status == "ok"]
    errors    = [r for r in phase_r if r.status == "error"]
    timeouts  = [r for r in phase_r if r.status == "timeout"]
    bp        = [r for r in phase_r if r.status == "backpressure"]

    lats = sorted(r.total_lat for r in ok)
    n    = len(phase_r)
    err_rate = (len(errors) + len(timeouts)) / n * 100 if n else 0.0

    return {
        "n_total":       n,
        "n_ok":          len(ok),
        "n_error":       len(errors),
        "n_timeout":     len(timeouts),
        "n_backpressure": len(bp),
        "error_rate_pct": round(err_rate, 2),
        "p50_s":   round(_percentile(lats, 50),  2),
        "p95_s":   round(_percentile(lats, 95),  2),
        "p99_s":   round(_percentile(lats, 99),  2),
        "avg_s":   round(sum(lats) / len(lats), 2) if lats else 0.0,
        "max_s":   round(max(lats), 2) if lats else 0.0,
    }


def _check_sla(stats: dict, phase_name: str) -> list[str]:
    """Return list of SLA breach messages (empty = pass)."""
    breaches = []
    if stats.get("p95_s", 0) > SLA["p95_s"]:
        breaches.append(
            f"{phase_name}: p95={stats['p95_s']:.1f}s > {SLA['p95_s']}s"
        )
    if stats.get("p99_s", 0) > SLA["p99_s"]:
        breaches.append(
            f"{phase_name}: p99={stats['p99_s']:.1f}s > {SLA['p99_s']}s"
        )
    if stats.get("error_rate_pct", 0) > SLA["error_rate_pct"]:
        breaches.append(
            f"{phase_name}: err%={stats['error_rate_pct']:.1f}% > {SLA['error_rate_pct']}%"
        )
    return breaches


# ─── Main ─────────────────────────────────────────────────────────────────────

async def _main_async(args: argparse.Namespace) -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

    # Parse phases
    if args.phases:
        raw = json.loads(args.phases)
        phases = [Phase(**p) for p in raw]
    else:
        phases = DEFAULT_PHASES

    cache_dir = Path(args.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    print(f"Loading image pool from {args.input} …")
    img_pool = _load_image_pool(args.input, cache_dir, limit=300)
    if not img_pool:
        print("ERROR: no images loaded -- check --input path and image URLs", file=sys.stderr)
        return 1
    print(f"Image pool: {len(img_pool)} images ready\n")

    all_results: list[RequestResult] = []

    for phase in phases:
        total_req = int(phase.rps * phase.duration_s)
        print(f"> Phase [{phase.name}]  {phase.rps} RPS × {phase.duration_s:.0f}s "
              f"≈ {total_req} requests …")
        await _run_phase(phase, img_pool, args.api, all_results)
        stats = _phase_stats(all_results, phase.name)
        breaches = _check_sla(stats, phase.name)
        icon = "✓ PASS" if not breaches else "✗ FAIL"
        print(f"  n={stats['n_total']:4d}  ok={stats['n_ok']:4d}  "
              f"err={stats['n_error']:3d}  bp={stats['n_backpressure']:3d}  "
              f"p50={stats['p50_s']:.1f}s  p95={stats['p95_s']:.1f}s  "
              f"p99={stats['p99_s']:.1f}s  err%={stats['error_rate_pct']:.1f}%  "
              f"{icon}")
        if breaches:
            for b in breaches:
                print(f"  ⚠  {b}")
        print()

    # Overall stats
    all_ok     = [r for r in all_results if r.status == "ok"]
    all_lats   = sorted(r.total_lat for r in all_ok)
    all_errors = [r for r in all_results if r.status in ("error", "timeout")]
    total_n    = len(all_results)
    err_rate   = len(all_errors) / total_n * 100 if total_n else 0.0

    overall_stats = {
        "n_total":        total_n,
        "n_ok":           len(all_ok),
        "n_error":        len(all_errors),
        "error_rate_pct": round(err_rate, 2),
        "p50_s":          round(_percentile(all_lats, 50), 2),
        "p95_s":          round(_percentile(all_lats, 95), 2),
        "p99_s":          round(_percentile(all_lats, 99), 2),
        "avg_s":          round(sum(all_lats) / len(all_lats), 2) if all_lats else 0.0,
    }
    all_breaches = _check_sla(overall_stats, "OVERALL")

    # Console summary table
    hdr = f"{'PHASE':15s}  {'n':>5}  {'p50':>6}  {'p95':>6}  {'p99':>6}  {'err%':>6}  SLA"
    print("=" * len(hdr))
    print("LOAD TEST SUMMARY")
    print("=" * len(hdr))
    print(hdr)
    print("-" * len(hdr))
    for phase in phases:
        s = _phase_stats(all_results, phase.name)
        b = _check_sla(s, phase.name)
        icon = "✓ PASS" if not b else "✗ FAIL"
        print(f"{phase.name:15s}  {s['n_total']:5d}  "
              f"{s['p50_s']:5.1f}s  {s['p95_s']:5.1f}s  {s['p99_s']:5.1f}s  "
              f"{s['error_rate_pct']:5.1f}%  {icon}")
    print("-" * len(hdr))
    icon = "✓ PASS" if not all_breaches else "✗ FAIL"
    print(f"{'OVERALL':15s}  {overall_stats['n_total']:5d}  "
          f"{overall_stats['p50_s']:5.1f}s  {overall_stats['p95_s']:5.1f}s  "
          f"{overall_stats['p99_s']:5.1f}s  "
          f"{overall_stats['error_rate_pct']:5.1f}%  {icon}")

    if all_breaches:
        print("\nSLA BREACHES:")
        for b in all_breaches:
            print(f"  ✗  {b}")

    # Write JSON report
    out_dir = Path(args.report)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = out_dir / f"load_test_report_{ts}.json"

    per_phase = {phase.name: _phase_stats(all_results, phase.name) for phase in phases}
    report = {
        "timestamp":  datetime.now().isoformat(timespec="seconds"),
        "api":        args.api,
        "sla":        SLA,
        "phases":     [{"name": p.name, "rps": p.rps, "duration_s": p.duration_s}
                       for p in phases],
        "per_phase":  per_phase,
        "overall":    overall_stats,
        "breaches":   all_breaches,
        "passed":     len(all_breaches) == 0,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    print(f"\nReport -> {report_path}")

    return 0 if not all_breaches else 1


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api",       default="http://localhost:8000")
    ap.add_argument("--input",     default="data/eval/dev_set.json",
                    help="Label file to sample images from")
    ap.add_argument("--report",    default="load_reports/",
                    help="Directory for JSON report output")
    ap.add_argument("--cache-dir", default="data/eval_images")
    ap.add_argument("--phases",    default=None,
                    help='JSON array of phases, e.g. \'[{"name":"smoke","rps":0.5,"duration_s":30}]\'')
    ap.add_argument("--seed",      type=int, default=42)
    args = ap.parse_args()

    random.seed(args.seed)
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
