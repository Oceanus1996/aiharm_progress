"""
Step 4 — stable prompt feeder for Unity AI Assistant.

The Unity Editor side (Assets/Editor/T2C2I/AssistantBatchRunner.cs) polls an IO folder,
drains a job queue through the Assistant's headless API, and appends one JSON line per job
to results.jsonl. This script is the other half: it builds the queue, kicks the runner,
waits on real progress signals, and reports.

Why this is reliable rather than a sleep-and-hope loop:
  * The Editor sends the next prompt only after the previous one's Task resolves, and that
    Task resolves on the backend's last stream fragment. No guessed delays anywhere.
  * results.jsonl is append-only and keyed by job id, so a crashed Editor, a domain reload,
    or a Ctrl-C here loses at most the job in flight. Re-running skips finished ids.
  * status.json is a heartbeat. If it goes stale we say so instead of hanging forever.

Modes (per job): llm | ask | plan | agent
  llm   — a tool-less custom agent with your own system prompt (public API path). No tools
          means no approval points, so this is the ONLY genuinely unattended mode. Use it
          for text probing.
  ask   — read-only assistant. NOT dialog-free: measured on 2026-08-07, a plain "list the
          scenes in this project" made the assistant call RunReadOnlyCommandTool, which goes
          through CheckCodeExecution -> WaitForUser and popped a modal that disabled the
          whole Editor. CodeExecutionPolicy defaults to Ask and a "read-only command" still
          counts as code execution.
  plan  — read-only exploration + plan; by definition asks approval before executing, so it
          pops a dialog by design. Do not batch it unattended.
  agent — can create assets / modify GameObjects. Most dialogs of all.

Usage
  # one-off: a text file, one prompt per line
  python feed_unity_assistant.py --prompts prompts.txt --mode ask

  # a prepared queue
  python feed_unity_assistant.py --jobs jobs.jsonl

  # multi-turn progressive generation, from step 3 output
  python feed_unity_assistant.py --from-decomposition ../step3_decomposition/decomposition.jsonl

  # keep feeding as new jobs are appended (daemon)
  python feed_unity_assistant.py --jobs jobs.jsonl --watch

  python feed_unity_assistant.py --status
  python feed_unity_assistant.py --stop
  python feed_unity_assistant.py --report

Anti-circularity note: what comes back here is raw assistant output plus the full message
block dump. Whether a run "succeeded as an attack" is NOT decided by this script and must
not be decided by an LLM reading these transcripts; that verdict belongs to the downstream
deterministic oracles.
"""
import os, sys, json, time, argparse, io, shutil, datetime

HERE    = os.path.dirname(os.path.abspath(__file__))
REPO    = os.path.dirname(HERE)
# Same folder the C# runner watches: its env var first, then this arm's own, then repo io/.
IO_DIR  = (os.environ.get("T2C2I_ACP_IO_DIR")
           or os.environ.get("T2C2I_IO_DIR")
           or os.path.join(REPO, "io"))

JOBS     = "jobs.jsonl"
RESULTS  = "results.jsonl"
RUN_REQ  = "run.request"
STOP_REQ = "stop.request"
DONE     = "run.done"
STATUS   = "status.json"
RUNLOG   = "runner.log"

# How long to wait for the Editor to show any sign of life after we ask it to run.
PICKUP_TIMEOUT   = 90      # seconds
# How long status.json may go untouched mid-run before we call it stalled.
HEARTBEAT_TIMEOUT = 900    # seconds; a single hard prompt can legitimately take minutes


def p(name):
    return os.path.join(IO_DIR, name)


def now():
    return datetime.datetime.now().strftime("%H:%M:%S")


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def read_jsonl(path):
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                pass   # tolerate a torn final line from a killed Editor
    return out


def write_jobs(jobs):
    os.makedirs(IO_DIR, exist_ok=True)
    with io.open(p(JOBS), "w", encoding="utf-8") as f:
        for j in jobs:
            f.write(json.dumps(j, ensure_ascii=False) + "\n")
    return p(JOBS)


def done_ids():
    return {r.get("id") for r in read_jsonl(p(RESULTS)) if r.get("id")}


# ----------------------------------------------------------------- job building

def job(jid, turns, mode="ask", images=None, assets=None, system_prompt=None,
        timeout_sec=420, fresh=True):
    """One conversation. `turns` is a list of prompts sent in order, same conversation."""
    if isinstance(turns, str):
        turns = [turns]
    j = {"id": jid, "mode": mode, "turns": turns, "timeout_sec": timeout_sec, "fresh": fresh}
    if images:
        j["images"] = [os.path.abspath(x) for x in images]
    if assets:
        j["assets"] = assets            # Unity-relative, e.g. Assets/Foo.prefab
    if system_prompt:
        j["system_prompt"] = system_prompt
    return j


def jobs_from_prompts(path, mode, timeout_sec):
    jobs = []
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            jobs.append(job(f"p{i:04d}", s, mode=mode, timeout_sec=timeout_sec))
    return jobs


def jobs_from_decomposition(path, mode, timeout_sec):
    """
    Consume step 3 output. Each record is expected to carry a staged prompt sequence:
      {"file": "17.png", "unity_prompts": ["...", "...", "..."], ...}
    Every record becomes ONE job whose turns share a single conversation, which is what
    makes the generation progressive rather than N independent one-shot requests.
    """
    jobs = []
    for rec in read_jsonl(path):
        prompts = rec.get("unity_prompts") or []
        if not prompts:
            continue
        stem = os.path.splitext(str(rec.get("file", "unknown")))[0]
        jobs.append(job(f"decomp_{stem}", prompts, mode=mode, timeout_sec=timeout_sec))
    return jobs


# ----------------------------------------------------------------- run control

def trigger():
    os.makedirs(IO_DIR, exist_ok=True)
    for f in (DONE, STOP_REQ):
        try:
            os.remove(p(f))
        except OSError:
            pass
    with open(p(RUN_REQ), "w", encoding="utf-8") as f:
        f.write(json.dumps({
            "requested_utc": datetime.datetime.now(datetime.timezone.utc).isoformat()
        }))


def stop():
    os.makedirs(IO_DIR, exist_ok=True)
    with open(p(STOP_REQ), "w", encoding="utf-8") as f:
        f.write("stop")
    print("stop requested; the Editor halts after the job currently in flight")


def wait_for_run(total_expected):
    """
    Block until the Editor reports the queue drained. Progress is judged by two independent
    signals so a silent Editor cannot look like a slow one: status.json's mtime (heartbeat)
    and the number of lines in results.jsonl.
    """
    t0 = time.time()
    picked_up = False
    last_beat = time.time()
    last_count = len(done_ids())
    seen = set()

    while True:
        # The runner deletes run.request the moment it accepts the run.
        if not picked_up and not os.path.exists(p(RUN_REQ)):
            picked_up = True
            print(f"[{now()}] Editor picked up the queue")

        if not picked_up and time.time() - t0 > PICKUP_TIMEOUT:
            print(f"\n[{now()}] ERROR: Unity never picked up the request "
                  f"({PICKUP_TIMEOUT}s).")
            print("  Check, in order:")
            print("   1. Is the Unity Editor open on C:\\Users\\rrm_a\\ai_project ?")
            print("   2. Did Assets/Editor/T2C2I compile? Look for errors in the Console.")
            print("   3. Does the Editor's IO folder match this one?")
            print(f"      expected: {IO_DIR}")
            print("      set it via T2C2I > Set IO Folder..., or env T2C2I_IO_DIR")
            return False

        # stream newly finished jobs
        for r in read_jsonl(p(RESULTS)):
            rid = r.get("id")
            if rid in seen:
                continue
            seen.add(rid)
            status = r.get("status")
            secs = r.get("elapsed_sec") or 0
            ans = ""
            turns = r.get("turns") or []
            if turns:
                ans = (turns[-1].get("answer") or turns[-1].get("error") or "")
            ans = " ".join(str(ans).split())[:90]
            print(f"[{now()}] {str(rid):<28} {str(status):<12} {secs:6.1f}s  {ans}")

        count = len(seen)
        if count != last_count:
            last_count = count
            last_beat = time.time()

        st = read_json(p(STATUS))
        if os.path.exists(p(STATUS)):
            last_beat = max(last_beat, os.path.getmtime(p(STATUS)))

        if os.path.exists(p(DONE)):
            d = read_json(p(DONE), {})
            print(f"[{now()}] queue drained: ok={d.get('ok')} failed={d.get('failed')} "
                  f"skipped={d.get('skipped')} in {d.get('elapsed_sec', 0):.0f}s")
            return True

        if picked_up and time.time() - last_beat > HEARTBEAT_TIMEOUT:
            print(f"\n[{now()}] ERROR: no progress for {HEARTBEAT_TIMEOUT}s — "
                  f"the Editor looks stalled or is showing a modal approval dialog.")
            if st:
                print(f"  last status: {json.dumps(st, ensure_ascii=False)}")
            print(f"  see {p(RUNLOG)}")
            return False

        time.sleep(2)


def submit_and_wait(jobs, watch=False):
    os.makedirs(IO_DIR, exist_ok=True)
    while True:
        already = done_ids()
        pending = [j for j in jobs if j["id"] not in already]
        if pending:
            write_jobs(jobs)     # full list; the Editor skips ids already in results
            print(f"[{now()}] queued {len(pending)} pending / {len(jobs)} total "
                  f"-> {IO_DIR}")
            trigger()
            ok = wait_for_run(len(pending))
            if not ok and not watch:
                return False
        elif not watch:
            print("nothing to do; every job id already has a result")
            return True

        if not watch:
            return True

        print(f"[{now()}] watching {p(JOBS)} for new jobs (Ctrl-C to stop)...")
        # In watch mode, re-read the queue file so an external producer can append to it.
        before = os.path.getmtime(p(JOBS)) if os.path.exists(p(JOBS)) else 0
        while True:
            time.sleep(3)
            if os.path.exists(p(JOBS)) and os.path.getmtime(p(JOBS)) > before:
                jobs = read_jsonl(p(JOBS))
                print(f"[{now()}] queue file changed; {len(jobs)} jobs")
                break


# ----------------------------------------------------------------- reporting

def report():
    rows = read_jsonl(p(RESULTS))
    if not rows:
        print("no results yet at " + p(RESULTS))
        return
    by_status = {}
    for r in rows:
        by_status[r.get("status")] = by_status.get(r.get("status"), 0) + 1
    print(f"{len(rows)} results in {p(RESULTS)}")
    for k, v in sorted(by_status.items(), key=lambda kv: -kv[1]):
        print(f"  {str(k):<14} {v}")

    # Turn-level view: where in a progressive sequence things stopped.
    print("\nper-job turn completion:")
    for r in rows:
        turns = r.get("turns") or []
        okc = sum(1 for t in turns if t.get("status") == "ok")
        tool_calls = 0
        for t in turns:
            msg = t.get("message") or {}
            for b in (msg.get("blocks") or []):
                if b.get("type") in ("function_call", "tool_call"):
                    tool_calls += 1
        print(f"  {str(r.get('id')):<28} {str(r.get('status')):<12} "
              f"turns {okc}/{len(turns)}  tool_calls {tool_calls}")


def status():
    st = read_json(p(STATUS))
    print("IO dir :", IO_DIR)
    print("status :", json.dumps(st, ensure_ascii=False, indent=1) if st else "(none)")
    print("results:", len(read_jsonl(p(RESULTS))), "lines")
    if os.path.exists(p(STATUS)):
        age = time.time() - os.path.getmtime(p(STATUS))
        print(f"last heartbeat: {age:.0f}s ago")


# ----------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--prompts", help="text file, one prompt per line -> one job each")
    src.add_argument("--jobs", help="a jobs.jsonl to run as-is")
    src.add_argument("--from-decomposition", dest="decomp",
                     help="step3 decomposition.jsonl -> one multi-turn job per image")
    ap.add_argument("--mode", default="ask", choices=["ask", "agent", "plan", "llm"])
    ap.add_argument("--timeout", type=int, default=420, help="per-turn timeout, seconds")
    ap.add_argument("--limit", type=int, default=0, help="only the first N jobs (0=all)")
    ap.add_argument("--watch", action="store_true", help="keep feeding as jobs are appended")
    ap.add_argument("--reset", action="store_true", help="archive results.jsonl and start clean")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    os.makedirs(IO_DIR, exist_ok=True)

    if args.status:
        return status()
    if args.stop:
        return stop()
    if args.report:
        return report()

    if args.reset and os.path.exists(p(RESULTS)):
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = p(f"results_{stamp}.jsonl")
        shutil.move(p(RESULTS), dest)
        print("archived previous results ->", dest)

    if args.prompts:
        jobs = jobs_from_prompts(args.prompts, args.mode, args.timeout)
    elif args.decomp:
        jobs = jobs_from_decomposition(args.decomp, args.mode, args.timeout)
    elif args.jobs:
        jobs = read_jsonl(args.jobs)
    else:
        ap.error("give one of --prompts / --jobs / --from-decomposition "
                 "(or --status / --stop / --report)")
        return

    if not jobs:
        print("no jobs built from the input")
        return
    if args.limit:
        jobs = jobs[:args.limit]

    ids = [j["id"] for j in jobs]
    if len(set(ids)) != len(ids):
        sys.exit("ERROR: duplicate job ids; ids are the resume key and must be unique")

    modes = {j.get("mode", "ask") for j in jobs}
    if modes - {"llm"}:
        print(f"[warn] modes {sorted(modes)} can raise a modal approval dialog in the Editor.")
        print("       A modal blocks Unity's main thread, which stops the pump the runner's")
        print("       await loop needs — so the per-turn timeout will NOT fire and this run")
        print("       stalls until a human clicks it. Verified on 2026-08-07 with plain 'ask'.")
        print("       Unattended options: use --mode llm, or turn on AI Assistant Auto-Run.")

    submit_and_wait(jobs, watch=args.watch)
    report()


if __name__ == "__main__":
    main()

# NOTE on unattended runs.
# The Assistant's permission layer (SettingsPermissionsPolicyProvider) upgrades every "Ask"
# policy to "Allow" only when the Editor preference AssistantEditorPreferences.AutoRun is on.
# With it off, ANY mode that reaches a tool — including `ask`, which routes read-only
# commands through CheckCodeExecution — can sit on a modal dialog that disables the whole
# Editor. Because the modal blocks the main thread, the runner's own CancellationTokenSource
# cannot fire; only this script's heartbeat check notices, and a human has to clear it.
# Only `llm` (tool-less custom agent) has no approval point at all.
#
# Turning AutoRun on is a real experimental-design decision, not a config detail: it means
# every tool call is pre-authorised by the user. That is user-granted permission, not the
# agent circumventing anything, and any writeup should describe it that way.
