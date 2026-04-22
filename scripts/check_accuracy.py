"""
check_accuracy.py — Accuracy gate for the CI pipeline.

Reads an eval_report JSON (produced by scripts/run_eval.py) and compares it
against experiments/baseline.json.  Exits 0 if all thresholds pass, 1 if any
fail.  On failure also writes a human-readable markdown summary to
accuracy_gate_result.md so GH Actions can render it inline.

Three layers of check (all must pass):

  1. Overall accuracy        — absolute floor + relative drop vs baseline
  2. Per-field accuracy      — absolute floor + relative drop vs baseline
  3. Per-store-type accuracy — absolute floor on the seven main fields avg

Two modes:

  --mode smoke    (default, used by stack-gate.yml on PRs)
                  Floors are relaxed by `ci_modes.smoke.floor_offset_pp`
                  and drop tolerances widened by `drop_multiplier`.
                  Rationale: PR smoke eval runs on 120 records so has
                  ~1-2pp sampling noise.

  --mode strict   (used by full-eval.yml on nightly + release tag)
                  Floors are the raw values in baseline.json.

Baseline bump (release tag only):

  --bump-baseline
                  Writes a NEW baseline.json from the current report,
                  floors set 2pp below observed numbers (to leave drop
                  headroom).  Only the CI workflow should call this, and
                  only on release tags.  Local developers should never
                  run this.

Usage:
    python scripts/check_accuracy.py \\
        --report eval_reports/eval_report_v3.7_test_*.json \\
        --baseline experiments/baseline.json \\
        [--mode smoke|strict] \\
        [--bump-baseline] \\
        [--out accuracy_gate_result.md]
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# Seven fields that define "per-store overall" for the by-type floor check.
PER_STORE_FIELDS = (
    "name", "date", "total_money", "receipt_number",
    "pos_id", "cashier", "product_name",
)


@dataclass
class Failure:
    kind:     str          # "overall_floor" | "overall_drop" | "field_floor" | ...
    subject:  str          # field name, store type, or "overall"
    metric:   str          # "pct" | "drop_pp"
    actual:   float
    threshold: float
    detail:   str = ""


@dataclass
class CheckResult:
    passed:   bool
    mode:     str
    failures: list[Failure] = field(default_factory=list)
    summary_lines: list[str] = field(default_factory=list)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _resolve_report_path(pattern: str) -> Path:
    """Glob the --report arg and return the most recent match."""
    matches = sorted(glob.glob(pattern))
    if not matches:
        raise FileNotFoundError(f"No report matches '{pattern}'")
    return Path(matches[-1])


def _load_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def _mode_tweaks(baseline: dict, mode: str) -> tuple[float, float]:
    cfg = baseline.get("ci_modes", {}).get(mode, {})
    return (
        float(cfg.get("floor_offset_pp", 0.0)),
        float(cfg.get("drop_multiplier", 1.0)),
    )


# ─── Core checks ─────────────────────────────────────────────────────────────

def _check_overall(report: dict, baseline: dict, ref: dict,
                   floor_offset: float, drop_mult: float) -> list[Failure]:
    fails: list[Failure] = []
    actual = float(report.get("overall_avg_pct", 0.0))
    floor  = float(baseline.get("overall_pct_floor", 0.0)) + floor_offset
    if actual < floor:
        fails.append(Failure(
            kind="overall_floor", subject="overall", metric="pct",
            actual=actual, threshold=floor,
        ))

    ref_overall = float(ref.get("overall_avg_pct", actual))
    drop_max = float(baseline.get("overall_pct_relative_drop_max", 999.0)) * drop_mult
    drop = ref_overall - actual
    if drop > drop_max:
        fails.append(Failure(
            kind="overall_drop", subject="overall", metric="drop_pp",
            actual=drop, threshold=drop_max,
            detail=f"ref={ref_overall:.1f}% current={actual:.1f}%",
        ))
    return fails


def _check_fields(report: dict, baseline: dict, ref: dict,
                  floor_offset: float, drop_mult: float) -> list[Failure]:
    fails: list[Failure] = []
    rep_fa  = report.get("field_accuracy", {})
    ref_fa  = ref.get("field_accuracy", {})
    floors  = baseline.get("field_floors", {})
    drops   = baseline.get("field_relative_drop_max", {})
    default_drop = float(drops.get("default", 1.5))

    for fname, floor in floors.items():
        actual = float(rep_fa.get(fname, {}).get("pct", 0.0))
        n_total = int(rep_fa.get(fname, {}).get("total", 0))
        adj_floor = float(floor) + floor_offset
        if n_total == 0:
            # Field not exercised in this run — skip rather than fail
            continue
        if actual < adj_floor:
            fails.append(Failure(
                kind="field_floor", subject=fname, metric="pct",
                actual=actual, threshold=adj_floor,
                detail=f"n={n_total}",
            ))
        ref_pct = float(ref_fa.get(fname, {}).get("pct", actual))
        allowed_drop = float(drops.get(fname, default_drop)) * drop_mult
        drop = ref_pct - actual
        if drop > allowed_drop:
            fails.append(Failure(
                kind="field_drop", subject=fname, metric="drop_pp",
                actual=drop, threshold=allowed_drop,
                detail=f"ref={ref_pct:.1f}% current={actual:.1f}%",
            ))
    return fails


def _check_stores(report: dict, baseline: dict,
                  floor_offset: float) -> list[Failure]:
    fails: list[Failure] = []
    cfg = baseline.get("by_store_floors", {})
    min_n = int(cfg.get("exclude_types_below_n", 10))
    floors = cfg.get("store_overall_floor", {})

    for store, info in (report.get("by_type") or {}).items():
        store_key = store.lower()
        n = int(info.get("n", 0))
        if n < min_n or store_key not in floors:
            continue
        fa = info.get("field_accuracy", {})
        pcts = [
            float(fa.get(f, {}).get("pct", 0.0))
            for f in PER_STORE_FIELDS
            if f in fa
        ]
        if not pcts:
            continue
        actual = sum(pcts) / len(pcts)
        floor  = float(floors[store_key]) + floor_offset
        if actual < floor:
            fails.append(Failure(
                kind="store_floor", subject=store_key, metric="avg_pct",
                actual=actual, threshold=floor,
                detail=f"n={n}",
            ))
    return fails


# ─── Report rendering ────────────────────────────────────────────────────────

def _render_summary(report: dict, baseline: dict, ref: dict,
                    result: CheckResult) -> str:
    lines: list[str] = []
    status = "✅ PASS" if result.passed else "❌ FAIL"
    lines.append(f"# Accuracy gate — {status}  (mode={result.mode})")
    lines.append("")
    lines.append(f"- psv         : `{report.get('psv')}`")
    lines.append(f"- split       : `{report.get('split')}`")
    lines.append(f"- n_total     : {report.get('n_total')}")
    lines.append(f"- git_sha     : `{report.get('git_sha','(none)')}`")
    lines.append(f"- baseline    : `{baseline.get('psv')}` "
                 f"(ref {ref.get('psv')}, overall {ref.get('overall_avg_pct')}%)")
    lines.append(f"- overall     : **{report.get('overall_avg_pct')}%** "
                 f"(floor {baseline.get('overall_pct_floor')}%)")
    lines.append("")

    if result.failures:
        lines.append("## Failures")
        lines.append("")
        lines.append("| kind | subject | actual | threshold | detail |")
        lines.append("|------|---------|-------:|----------:|--------|")
        for f in result.failures:
            arrow = "%" if f.metric == "pct" or f.metric == "avg_pct" else "pp"
            lines.append(
                f"| {f.kind} | `{f.subject}` | {f.actual:.2f}{arrow} "
                f"| {f.threshold:.2f}{arrow} | {f.detail} |"
            )
        lines.append("")
    else:
        lines.append("All assertions passed.")
        lines.append("")

    # Per-field table for context
    lines.append("## Per-field accuracy")
    lines.append("")
    lines.append("| field | current | ref | Δ | floor |")
    lines.append("|-------|--------:|----:|--:|------:|")
    rep_fa = report.get("field_accuracy", {})
    ref_fa = ref.get("field_accuracy", {})
    floors = baseline.get("field_floors", {})
    for fname in sorted(set(rep_fa) | set(floors)):
        cur = rep_fa.get(fname, {}).get("pct")
        rv  = ref_fa.get(fname, {}).get("pct")
        fl  = floors.get(fname)
        cur_s = f"{cur:.1f}%" if cur is not None else "—"
        rv_s  = f"{rv:.1f}%"  if rv  is not None else "—"
        fl_s  = f"{fl:.1f}%"  if fl  is not None else "—"
        if cur is not None and rv is not None:
            d_s = f"{cur - rv:+.1f}pp"
        else:
            d_s = "—"
        lines.append(f"| {fname} | {cur_s} | {rv_s} | {d_s} | {fl_s} |")
    lines.append("")

    return "\n".join(lines)


# ─── Baseline bump ───────────────────────────────────────────────────────────

def _bump_baseline(report: dict, baseline_path: Path, report_path: Path) -> None:
    """Rewrite baseline.json using the current report.
    Floors = observed - 2pp, clamped to [50, 100].
    """
    new_floors: dict[str, float] = {}
    for fname, fa in (report.get("field_accuracy") or {}).items():
        pct = float(fa.get("pct", 0.0))
        new_floors[fname] = round(max(50.0, min(100.0, pct - 2.0)), 1)

    # Per-store floor = avg of PER_STORE_FIELDS minus 3pp
    store_floors: dict[str, float] = {}
    for store, info in (report.get("by_type") or {}).items():
        if int(info.get("n", 0)) < 10:
            continue
        fa = info.get("field_accuracy", {})
        pcts = [
            float(fa.get(f, {}).get("pct", 0.0))
            for f in PER_STORE_FIELDS
            if f in fa
        ]
        if not pcts:
            continue
        store_floors[store.lower()] = round(max(50.0, sum(pcts) / len(pcts) - 3.0), 1)

    overall = float(report.get("overall_avg_pct", 0.0))
    current = _load_json(baseline_path)

    new_baseline = {
        "_doc": current.get("_doc", ""),
        "psv":          report.get("psv"),
        "committed_at": datetime.now().date().isoformat(),
        "reference_run": str(report_path),
        "overall_pct_floor":            round(max(50.0, overall - 2.0), 1),
        "overall_pct_relative_drop_max": current.get("overall_pct_relative_drop_max", 1.5),
        "field_floors":                new_floors,
        "field_relative_drop_max":     current.get("field_relative_drop_max", {"default": 1.5}),
        "by_store_floors": {
            "_doc": current.get("by_store_floors", {}).get("_doc", ""),
            "exclude_types_below_n": 10,
            "store_overall_floor":   store_floors,
        },
        "ci_modes": current.get("ci_modes", {}),
    }

    baseline_path.write_text(
        json.dumps(new_baseline, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


# ─── Main ────────────────────────────────────────────────────────────────────

def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--report",   required=True,
                    help="Path or glob to the eval_report JSON")
    ap.add_argument("--baseline", default="experiments/baseline.json")
    ap.add_argument("--mode",     default="smoke", choices=["smoke", "strict"])
    ap.add_argument("--out",      default="accuracy_gate_result.md",
                    help="Markdown summary output (always written)")
    ap.add_argument("--bump-baseline", action="store_true",
                    help="Rewrite baseline.json from current report. "
                         "Intended for release-tag CI only.")
    args = ap.parse_args()

    report_path = _resolve_report_path(args.report)
    baseline_path = Path(args.baseline)
    if not baseline_path.exists():
        print(f"ERROR: baseline not found: {baseline_path}", file=sys.stderr)
        return 2

    report   = _load_json(report_path)
    baseline = _load_json(baseline_path)

    ref_path = Path(baseline.get("reference_run", ""))
    ref = _load_json(ref_path) if ref_path.exists() else {}

    # Bump mode short-circuits the check — it just rewrites baseline.
    if args.bump_baseline:
        _bump_baseline(report, baseline_path, report_path)
        print(f"Baseline bumped from report {report_path} -> {baseline_path}")
        return 0

    floor_offset, drop_mult = _mode_tweaks(baseline, args.mode)

    failures: list[Failure] = []
    failures += _check_overall(report, baseline, ref, floor_offset, drop_mult)
    failures += _check_fields(report,  baseline, ref, floor_offset, drop_mult)
    failures += _check_stores(report,  baseline, floor_offset)

    result = CheckResult(passed=not failures, mode=args.mode, failures=failures)

    summary = _render_summary(report, baseline, ref, result)
    Path(args.out).write_text(summary, encoding="utf-8")

    # Console one-liner
    if result.passed:
        print(
            f"ACCURACY OK [{args.mode}] — overall {report.get('overall_avg_pct')}% "
            f"(floor {baseline.get('overall_pct_floor')}%, "
            f"ref {ref.get('overall_avg_pct', '—')}%)"
        )
        return 0
    else:
        print(
            f"ACCURACY FAIL [{args.mode}] — {len(failures)} breach(es). "
            f"See {args.out}",
            file=sys.stderr,
        )
        for f in failures[:20]:
            unit = "pp" if f.metric.endswith("drop_pp") else "%"
            print(
                f"  - {f.kind:14s}  {f.subject:16s}  "
                f"actual={f.actual:.2f}{unit}  threshold={f.threshold:.2f}{unit}"
                f"  {f.detail}",
                file=sys.stderr,
            )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
