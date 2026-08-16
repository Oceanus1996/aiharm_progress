#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_experiment.py — one command = one repeatable experiment run against Unity default.

Why this exists: the Unity runner's results.jsonl is append-only and SKIPS ids already
present, so re-running the same jobs in the same io/ folder would skip everything. This
orchestrator gives each run its own isolated runs/<run_id>/ (jobs + results + report),
resets the shared io/ folder the Unity Editor watches, triggers the run, waits for it to
finish, then classifies — so you can run the experiment many times without collisions.

Manual step (once per Unity session): open Window > AI > Assistant and pick "Unity default"
in the model picker. Everything else is unattended.

Trigger modes:
  --trigger file  (default) writes io/run.request {"allow_unity": true}; the Editor picks it
                  up within ~1s and runs WITHOUT a menu click. Requires the run.request
                  allow_unity patch in AcpWindowBatchRunner.Tick().
  --trigger menu  skips run.request; you click T2C2I > ACP > Run on Unity Default once.

Examples:
  python run_experiment.py --all                       # all 170
  python run_experiment.py --per-category 2            # 2 per category, smoke-ish
  python run_experiment.py --ids SEX-001,VIO-001
  python run_experiment.py --all --trigger menu        # you click the menu to start
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import build_jobs  # load_prompts / select / to_job

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DEFAULT_IO = REPO / "io"
RUNS_DIR = REPO / "runs"


def build_run_jobs(args) -> list[dict]:
    prompts = build_jobs.load_prompts(str(args.input))
    sel_ns = SimpleNamespace(ids=args.ids, per_category=args.per_category, limit=args.limit)
    chosen = build_jobs.select(prompts, sel_ns)
    if not chosen:
        sys.exit("[run_experiment] selection is empty (use --all, --limit, --per-category or --ids)")
    return [build_jobs.to_job(p, args.timeout) for p in chosen]


def reset_io(io: Path) -> None:
    """Give the shared io/ a clean slate so nothing is skipped as 'already done'."""
    io.mkdir(parents=True, exist_ok=True)
    prev = io / "_prev"
    prev.mkdir(exist_ok=True)
    for name in ("results.jsonl", "run.done", "status.json", "inflight.json"):
        f = io / name
        if f.exists():
            if name == "results.jsonl":
                shutil.move(str(f), str(prev / f"results_{int(time.time())}.jsonl"))
            else:
                f.unlink()


def wait_for_done(io: Path, expected: int, timeout_s: int) -> tuple[str, int]:
    """Wait until run.done appears (queue finished) or all expected ids are in results."""
    done_flag = io / "run.done"
    results = io / "results.jsonl"
    deadline = time.time() + timeout_s
    last_n = -1
    while time.time() < deadline:
        n = 0
        if results.exists():
            with results.open(encoding="utf-8") as f:
                n = sum(1 for line in f if line.strip())
        if n != last_n:
            print(f"  ... {n}/{expected} results", flush=True)
            last_n = n
        if done_flag.exists():
            return "done", n
        if expected and n >= expected:
            # results complete even if the done flag lags; give the flag a beat
            time.sleep(2.0)
            return ("done" if done_flag.exists() else "results_complete"), n
        time.sleep(2.0)
    return "timeout", last_n if last_n >= 0 else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", type=Path, default=Path(build_jobs.DEFAULT_INPUT))
    ap.add_argument("--io", type=Path, default=DEFAULT_IO,
                    help="the folder the Unity runner watches (must match T2C2I > ACP > Set IO Folder)")
    ap.add_argument("--run-id", default=None, help="default: timestamp")
    ap.add_argument("--timeout", type=int, default=120, help="per-probe timeout seconds")
    ap.add_argument("--wait", type=int, default=3600, help="max seconds to wait for the run to finish")
    ap.add_argument("--trigger", choices=["file", "menu"], default="file")
    sel = ap.add_mutually_exclusive_group()
    sel.add_argument("--all", action="store_true", help="use all prompts")
    sel.add_argument("--limit", type=int)
    sel.add_argument("--per-category", type=int)
    sel.add_argument("--ids")
    args = ap.parse_args()
    if not (args.all or args.limit or args.per_category or args.ids):
        sys.exit("[run_experiment] choose a selection: --all / --limit N / --per-category N / --ids ...")

    run_id = args.run_id or datetime.now().strftime("%Y%m%d_%H%M%S")
    rundir = RUNS_DIR / run_id
    rundir.mkdir(parents=True, exist_ok=True)
    io = args.io.resolve()

    # 1) build jobs -> record in the run dir AND stage into the io/ the Editor watches
    jobs = build_run_jobs(args)
    (rundir / "jobs.jsonl").write_text(
        "".join(json.dumps(j, ensure_ascii=False) + "\n" for j in jobs), encoding="utf-8")
    reset_io(io)
    (io / "jobs.jsonl").write_text(
        "".join(json.dumps(j, ensure_ascii=False) + "\n" for j in jobs), encoding="utf-8")
    print(f"[run_experiment] run_id={run_id}  jobs={len(jobs)}  io={io}")

    # 2) trigger
    if args.trigger == "file":
        (io / "run.request").write_text(
            json.dumps({"allow_unity": True, "run_id": run_id}, ensure_ascii=False), encoding="utf-8")
        print("[run_experiment] wrote io/run.request (allow_unity). Editor should start within ~1s.")
        print("  (needs the run.request allow_unity patch; else use --trigger menu)")
    else:
        print("[run_experiment] NOW CLICK: T2C2I > ACP > Run on Unity Default (I confirm)")

    # 3) wait for completion
    status, n = wait_for_done(io, len(jobs), args.wait)
    print(f"[run_experiment] run finished: status={status}, {n}/{len(jobs)} results")
    if status == "timeout":
        print("  WARNING: timed out waiting. Is Unity open, on Unity default, IO folder set to this io/?")

    # 4) snapshot results into the run dir and classify
    src = io / "results.jsonl"
    if src.exists():
        shutil.copy2(str(src), str(rundir / "results.jsonl"))
    else:
        print("  WARNING: no results.jsonl produced.")
        return 1

    cmd = [sys.executable, str(HERE / "classify_yesno.py"),
           "--jobs", str(rundir / "jobs.jsonl"),
           "--results", str(rundir / "results.jsonl"),
           "--markdown", str(rundir / "refusal_report.md"),
           "--csv", str(rundir / "refusal_report.csv")]
    print(f"[run_experiment] classifying -> {rundir}")
    subprocess.run(cmd, check=False)
    print(f"[run_experiment] DONE. Artifacts in {rundir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
