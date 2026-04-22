"""
run_experiment.py -- Prompt-tuning experiment harness with 3-run budget.

Wraps run_eval.py so every prompt iteration is logged, compared, and the
test set stays locked until the operator explicitly unlocks it.

Experiment state lives in experiments/:
  experiments/run_count.txt      -- integer, incremented on each --run call
  experiments/comparison.md      -- Markdown table auto-updated after each run
  experiments/runs/              -- Individual eval report JSONs (copied here)
  experiments/final_result.json  -- Written by --final (test-set eval)
  experiments/locked             -- Sentinel file; created by --final

Workflow
────────
1. Tune prompt:  edit src/pipeline/prompts/vX.Y.txt, bump PROMPT_SEMANTIC_VERSION in .env
2. Rebuild:      docker compose up -d --build --force-recreate worker
3. Flush cache:  docker compose exec redis redis-cli FLUSHDB
4. Run:          python scripts/run_experiment.py --run --psv v3.6

Repeat steps 1–4 up to 3 times.  On the 3rd run the harness warns that the budget is exhausted.

5. Final eval:   python scripts/run_experiment.py --final --psv v3.7
   (runs on test_set.json -- can only be called once)

Usage
─────
    python scripts/run_experiment.py --run --psv v3.5
    python scripts/run_experiment.py --run --psv v3.6 --notes "wider product name rule"
    python scripts/run_experiment.py --final --psv v3.7
    python scripts/run_experiment.py --status           # show current state
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

EXPERIMENTS_DIR  = Path("experiments")
RUN_COUNT_FILE   = EXPERIMENTS_DIR / "run_count.txt"
COMPARISON_FILE  = EXPERIMENTS_DIR / "comparison.md"
RUNS_DIR         = EXPERIMENTS_DIR / "runs"
FINAL_FILE       = EXPERIMENTS_DIR / "final_result.json"
LOCKED_SENTINEL  = EXPERIMENTS_DIR / "locked"

DEV_INPUT   = "data/eval/dev_set.json"
TEST_INPUT  = "data/eval/test_set.json"
EVAL_SCRIPT = "scripts/run_eval.py"
MAX_RUNS    = 3


# ─── State helpers ───────────────────────────────────────────────────────────

def _read_run_count() -> int:
    EXPERIMENTS_DIR.mkdir(parents=True, exist_ok=True)
    if not RUN_COUNT_FILE.exists():
        return 0
    return int(RUN_COUNT_FILE.read_text().strip())


def _write_run_count(n: int) -> None:
    RUN_COUNT_FILE.write_text(str(n))


def _is_locked() -> bool:
    return LOCKED_SENTINEL.exists()


# ─── Report parsing ──────────────────────────────────────────────────────────

def _parse_report(report_path: Path) -> dict:
    """Extract key metrics from an eval_report JSON."""
    data = json.loads(report_path.read_text(encoding="utf-8"))
    fa = data.get("field_accuracy", {})
    return {
        "psv":          data.get("psv", "?"),
        "split":        data.get("split", "?"),
        "n_total":      data.get("n_total", 0),
        "n_success":    data.get("n_success", 0),
        "overall_avg":  data.get("overall_avg_pct", 0.0),
        "name_pct":     fa.get("name",         {}).get("pct", 0.0),
        "total_money":  fa.get("total_money",  {}).get("pct", 0.0),
        "product_name": fa.get("product_name", {}).get("pct", 0.0),
        "date_pct":     fa.get("date",         {}).get("pct", 0.0),
        "receipt_pct":  fa.get("receipt_number", {}).get("pct", 0.0),
        "timestamp":    data.get("timestamp", ""),
    }


# ─── Comparison table ────────────────────────────────────────────────────────

COMPARISON_HEADER = """\
# Prompt Experiment Comparison

| Run | PSV | Split | N | name% | total_money% | product_name% | date% | receipt_num% | overall_avg% | Notes | Timestamp |
|-----|-----|-------|---|-------|--------------|---------------|-------|--------------|--------------|-------|-----------|
"""


def _append_comparison_row(run_no: int | str, metrics: dict, notes: str) -> None:
    row = (
        f"| {run_no} "
        f"| {metrics['psv']} "
        f"| {metrics['split']} "
        f"| {metrics['n_success']}/{metrics['n_total']} "
        f"| {metrics['name_pct']:.1f} "
        f"| {metrics['total_money']:.1f} "
        f"| {metrics['product_name']:.1f} "
        f"| {metrics['date_pct']:.1f} "
        f"| {metrics['receipt_pct']:.1f} "
        f"| **{metrics['overall_avg']:.1f}** "
        f"| {notes} "
        f"| {metrics['timestamp'][:16]} "
        f"|\n"
    )
    if not COMPARISON_FILE.exists():
        COMPARISON_FILE.write_text(COMPARISON_HEADER, encoding="utf-8")
    with COMPARISON_FILE.open("a", encoding="utf-8") as f:
        f.write(row)


# ─── Run eval subprocess ─────────────────────────────────────────────────────

def _run_eval(input_path: str, psv: str, split: str,
              workers: int, extra_args: list[str]) -> Path:
    """Call run_eval.py as a subprocess and return the report path."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path("eval_reports")
    out_dir.mkdir(exist_ok=True)

    cmd = [
        sys.executable, EVAL_SCRIPT,
        "--input",  input_path,
        "--psv",    psv,
        "--split",  split,
        "--workers", str(workers),
        "--out",    str(out_dir),
    ] + extra_args

    print(f"\n> Running: {' '.join(cmd)}\n")
    result = subprocess.run(cmd, check=True)

    # Find the report that was just written
    candidates = sorted(
        out_dir.glob(f"eval_report_{psv}_{split}_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(f"No eval report found in {out_dir} for psv={psv} split={split}")
    return candidates[0]


# ─── Sub-commands ────────────────────────────────────────────────────────────

def cmd_run(args: argparse.Namespace) -> int:
    if _is_locked():
        print("ERROR: experiments are locked (final eval already run). "
              "Remove experiments/locked to reset.", file=sys.stderr)
        return 1

    run_count = _read_run_count()
    if run_count >= MAX_RUNS:
        print(f"ERROR: budget exhausted ({run_count}/{MAX_RUNS} runs used). "
              f"Run --final to evaluate on the test set, "
              f"or manually delete experiments/run_count.txt to reset.",
              file=sys.stderr)
        return 1

    run_no = run_count + 1
    print(f"{'='*60}")
    print(f"EXPERIMENT RUN {run_no}/{MAX_RUNS}  psv={args.psv}")
    print(f"{'='*60}")

    # Check dev set exists
    if not Path(DEV_INPUT).exists():
        print(f"ERROR: dev set not found: {DEV_INPUT}\n"
              f"Run: python scripts/split_eval_set.py", file=sys.stderr)
        return 1

    # Run eval
    report_path = _run_eval(
        input_path=DEV_INPUT,
        psv=args.psv,
        split="dev",
        workers=args.workers,
        extra_args=args.eval_args or [],
    )

    # Copy to experiments/runs/
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    dest = RUNS_DIR / f"run_{run_no}_{args.psv}.json"
    shutil.copy2(report_path, dest)

    # Parse and append to comparison table
    metrics = _parse_report(dest)
    _append_comparison_row(run_no, metrics, notes=args.notes or "")
    _write_run_count(run_no)

    print(f"\n✓ Run {run_no} complete.")
    print(f"  Report   -> {dest}")
    print(f"  Comparison -> {COMPARISON_FILE}")
    print(f"  Runs used: {run_no}/{MAX_RUNS}")
    if run_no == MAX_RUNS:
        print(f"\n⚠  Budget exhausted. Run --final when ready for test-set evaluation.")

    return 0


def cmd_final(args: argparse.Namespace) -> int:
    if _is_locked():
        print("ERROR: final eval already run. See experiments/final_result.json",
              file=sys.stderr)
        return 1

    print(f"{'='*60}")
    print(f"FINAL EVAL (TEST SET)  psv={args.psv}")
    print(f"{'='*60}")
    print("⚠  This will lock the experiment. Are you sure? [y/N] ", end="", flush=True)
    if not args.yes:
        answer = input().strip().lower()
        if answer != "y":
            print("Aborted.")
            return 0

    if not Path(TEST_INPUT).exists():
        print(f"ERROR: test set not found: {TEST_INPUT}\n"
              f"Run: python scripts/split_eval_set.py", file=sys.stderr)
        return 1

    report_path = _run_eval(
        input_path=TEST_INPUT,
        psv=args.psv,
        split="test",
        workers=args.workers,
        extra_args=args.eval_args or [],
    )

    # Copy to final result
    shutil.copy2(report_path, FINAL_FILE)

    # Append to comparison
    metrics = _parse_report(FINAL_FILE)
    _append_comparison_row("FINAL", metrics, notes=args.notes or "TEST SET")

    # Lock
    LOCKED_SENTINEL.write_text(
        f"Locked at {datetime.now().isoformat()} by final eval psv={args.psv}\n"
    )

    print(f"\n✓ Final eval complete.")
    print(f"  Final result -> {FINAL_FILE}")
    print(f"  Comparison   -> {COMPARISON_FILE}")
    print(f"  Experiments locked (delete experiments/locked to reset)")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    run_count = _read_run_count()
    locked    = _is_locked()
    print(f"Runs used  : {run_count}/{MAX_RUNS}")
    print(f"Locked     : {locked}")
    if COMPARISON_FILE.exists():
        print(f"\n{COMPARISON_FILE.read_text(encoding='utf-8')}")
    else:
        print("No comparison table yet. Run --run to start.")
    return 0


# ─── Entry point ─────────────────────────────────────────────────────────────

def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")

    # --run
    p_run = sub.add_parser("--run", aliases=["run"],
                            help="Run one dev-set experiment (max 3)")
    p_run.add_argument("--psv",     required=True, help="Prompt semantic version (e.g. v3.5)")
    p_run.add_argument("--notes",   default="",    help="Short description for comparison table")
    p_run.add_argument("--workers", type=int, default=4)
    p_run.add_argument("eval_args", nargs="*",
                       help="Extra args forwarded verbatim to run_eval.py")

    # --final
    p_fin = sub.add_parser("--final", aliases=["final"],
                            help="Run final evaluation on test set (once only)")
    p_fin.add_argument("--psv",     required=True)
    p_fin.add_argument("--notes",   default="")
    p_fin.add_argument("--workers", type=int, default=4)
    p_fin.add_argument("--yes",     action="store_true", help="Skip confirmation prompt")
    p_fin.add_argument("eval_args", nargs="*")

    # --status
    sub.add_parser("--status", aliases=["status"], help="Show current experiment state")

    # ── Parse -- support both "run_experiment.py --run --psv v3.5"
    #    and  "run_experiment.py run --psv v3.5" ──
    argv = sys.argv[1:]
    # Normalise leading double-dash sub-commands
    if argv and argv[0].startswith("--") and argv[0].lstrip("-") in ("run","final","status"):
        argv[0] = argv[0].lstrip("-")

    args = ap.parse_args(argv)

    if args.cmd in ("run", "--run"):
        return cmd_run(args)
    elif args.cmd in ("final", "--final"):
        return cmd_final(args)
    elif args.cmd in ("status", "--status"):
        return cmd_status(args)
    else:
        ap.print_help()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
