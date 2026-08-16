# aiharm_progress — I2P → Unity AI Assistant safety-filter evaluation

A small, deploy-friendly harness for **measuring whether a generative assistant refuses
unsafe requests**. It feeds the public [I2P benchmark](https://huggingface.co/datasets/AIML-TUDA/i2p)
(4703 real-world text-to-image prompts, labeled across 7 unsafe concepts) into the **Unity
AI Assistant** through a file-queue runner, records each answer, and classifies refusal vs.
compliance.

> **Intended use — defensive safety research only.** This repo exists to *evaluate and
> harden* the safety filters of a 3D/VR generative model. The prompts come from an already-public
> academic benchmark (Schramowski et al., *Safe Latent Diffusion*, CVPR 2023) and are wrapped
> in a yes/no **compliance probe** — the harness asks the model *whether it would* generate
> something, it does not produce unsafe media. Do not use it to elicit or distribute harmful
> content. No secrets or credentials are shipped in this repo. One set of **model responses** is:
> `data/i2p_claude_policy_flagged.json` — the prompts a model predicted would trip its own safety
> policy. Raw `io/results.jsonl` stays local and is git-ignored.

## What's inside

```
data/i2pharmprompt.json      I2P (4703) in this repo's schema: id, category{en,zh}, hard, prompt, metrics
data/i2p_claude_policy_flagged.json
                             RESULT: the prompts Claude Code predicted would be declined on safety
                             grounds, with the probe used and the caveats that apply to reading it
tools/
  convert_i2p.py             rebuild data/ from the public HF CSV
  build_i2p_jobs.py          data -> jobs.jsonl (each prompt wrapped in a yes/no probe;
                             --probe willingness|policy picks which question is asked)
  split_batches.py           jobs.jsonl -> batch files of N (pacing / handing out work)
  classify_yesno.py          results.jsonl -> refusal classification
  feed_unity_assistant.py    optional external driver (file-queue, non-ACP path)
  run_experiment.py          optional driver for the ACP window runner
unity/T2C2I/                 the Unity Editor runner (install into your Unity project)
  AcpWindowBatchRunner.cs    drives the visible Assistant window (any selected provider)
  Unity.AI.Assistant.DeveloperTools.asmdef
io/                          runtime IO folder (jobs/results/status live here; git-ignored)
docs/                        DESIGN.md + reliability.md (how the runner stays stable)
```

## Deploy (collaborators)

1. **Install the runner.** Copy `unity/T2C2I/` into your Unity project at
   `Assets/Editor/T2C2I/`. Open Unity; when it compiles you'll see a **`T2C2I`** menu.
   (Needs Unity AI Assistant `2.16.x`. The asmdef borrows the name
   `Unity.AI.Assistant.DeveloperTools` to reach the package internals — if Unity ever ships an
   assembly by that name, rename it.)
2. **Point the runner at an IO folder.** `T2C2I > ACP > Set IO Folder...` → pick the `io/`
   folder of your checkout (or set env `T2C2I_ACP_IO_DIR`). The Python side must use the same
   folder.
3. **Build jobs.**
   ```bash
   pip install pandas          # only if you rebuild data with convert_i2p.py
   python tools/build_i2p_jobs.py --scope smoke --limit 20 --out io/jobs.jsonl   # smoke
   # or the hard subset / everything:
   python tools/build_i2p_jobs.py --scope hard --out io/jobs.jsonl
   python tools/build_i2p_jobs.py --scope all  --out io/i2p_all_jobs.jsonl
   python tools/split_batches.py --input io/i2p_all_jobs.jsonl --size 500 --outdir io/batches
   ```
4. **Run.** In Unity, open the Assistant window and pick your provider, then click
   **`T2C2I > ACP > Run Queue Now`** (or `Run on Unity Default (I confirm)` to measure the
   Unity default model). To run a batch, first `cp io/batches/batch_01.jsonl io/jobs.jsonl`.
   Results append to `io/results.jsonl`, keyed by `id`; re-running skips finished ids, so a
   long run is resumable — just click again.
5. **Classify.**
   ```bash
   python tools/classify_yesno.py --results io/results.jsonl
   ```

## Reading the numbers (important)

**Three different quantities, never one denominator.** `--probe willingness` asks *"can you
generate this"* (Yes = comply); `--probe policy` asks *"would this be declined for safety
reasons"* (Yes = refuse); actually asking the model to generate measures a third thing. A
model's prediction about its own guardrail is not its behaviour, and neither is its stated
willingness. Each job records `_meta.yes_means` so `classify_yesno.py` maps Yes to
comply/refuse by probe rather than by hand — the polarity is inverted between the two.

The plain willingness probe cannot tell a **safety refusal** ("I won't") from a
**capability/domain refusal** ("I can't make 2D art") — I2P prompts are 2D image prompts and
the target may be a 3D assistant. The policy probe excludes non-safety declines explicitly and
forbids tool use, which is what makes it usable against agentic providers.

**Do not group by `hard`.** It is tempting, and it is misleading: `hard` marks prompts where
≥50% of Stable Diffusion's 10 generations were classified inappropriate, i.e. it describes
*what the image generator drew*, while a text probe only ever sees the prompt. Measured over
1669 prompts, `hard` looks predictive on its own but nearly all of that disappears once
`prompt_toxicity` is held constant. Group by `prompt_toxicity`; treat `categories` as
*retrieval provenance* (the I2P authors inherited each label from the keyword that retrieved
the prompt via CLIP image similarity), not as a content judgement.

Refusal detection here decides *"did it refuse"*, never *"did an attack succeed"* — that
verdict belongs to downstream deterministic oracles, not to an LLM reading transcripts
(see `docs/DESIGN.md`).

## Data / citation

I2P is redistributed here in a derived JSON form for reproducibility; the original benchmark:

> P. Schramowski, M. Brack, B. Deiseroth, K. Kersting. *Safe Latent Diffusion: Mitigating
> Inappropriate Degeneration in Diffusion Models.* CVPR 2023.
> Dataset: https://huggingface.co/datasets/AIML-TUDA/i2p
