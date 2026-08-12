#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
split_batches.py — split a jobs.jsonl into fixed-size batch files.

The Unity runner drains one jobs.jsonl per run and skips ids already in results.jsonl,
so batching is just a convenience for pacing a long run (and for handing different batches
to different collaborators). Run one batch by copying it over the runner's jobs.jsonl:

    python tools/build_i2p_jobs.py --scope all --out io/i2p_all_jobs.jsonl
    python tools/split_batches.py --input io/i2p_all_jobs.jsonl --size 500 --outdir io/batches
    cp io/batches/batch_01.jsonl io/jobs.jsonl      # then click T2C2I > ACP > Run ...
"""
import argparse, os


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="a jobs.jsonl to split")
    ap.add_argument("--size", type=int, default=500, help="jobs per batch")
    ap.add_argument("--outdir", default="io/batches")
    args = ap.parse_args()

    with open(args.input, encoding="utf-8") as f:
        lines = [l for l in f if l.strip()]

    os.makedirs(args.outdir, exist_ok=True)
    n = 0
    for i in range(0, len(lines), args.size):
        n += 1
        out = os.path.join(args.outdir, f"batch_{n:02d}.jsonl")
        with open(out, "w", encoding="utf-8") as f:
            f.writelines(lines[i:i + args.size])
        print(f"{out} : {len(lines[i:i + args.size])} jobs")
    print(f"total {len(lines)} -> {n} batches in {args.outdir}")


if __name__ == "__main__":
    main()
