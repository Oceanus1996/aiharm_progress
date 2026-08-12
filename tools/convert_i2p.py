# -*- coding: utf-8 -*-
"""
convert_i2p.py — rebuild data/i2pharmprompt.json from the public I2P benchmark CSV.

Downloads (or reads) AIML-TUDA/i2p (i2p_benchmark.csv, 4703 rows) and emits the JSON
structure used by this repo: per-prompt {id, category:{en,zh}, hard, prompt:{en,zh},
metrics, source, sd_generation}.

    # fetch the CSV first (once):
    #   curl -L -o i2p_benchmark.csv \
    #     https://huggingface.co/datasets/AIML-TUDA/i2p/resolve/main/i2p_benchmark.csv
    python tools/convert_i2p.py --csv i2p_benchmark.csv --out data/i2pharmprompt.json
"""
import argparse, json, math
import pandas as pd

CAT_ZH = {
    "hate": "仇恨", "harassment": "骚扰", "violence": "暴力", "self-harm": "自残",
    "sexual": "色情", "shocking": "令人震惊", "illegal activity": "非法活动",
}


def as_float(v):
    try:
        f = float(v); return None if math.isnan(f) else f
    except Exception:
        return None


def as_int(v):
    try:
        f = float(v); return None if math.isnan(f) else int(f)
    except Exception:
        return None


def as_str(v):
    if v is None:
        return None
    try:
        if isinstance(v, float) and math.isnan(v):
            return None
    except Exception:
        pass
    return str(v)


def split_cats(raw):
    if raw is None:
        return []
    try:
        if isinstance(raw, float) and math.isnan(raw):
            return []
    except Exception:
        pass
    return [c.strip() for c in str(raw).split(",") if c.strip()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="i2p_benchmark.csv")
    ap.add_argument("--out", default="data/i2pharmprompt.json")
    args = ap.parse_args()

    df = pd.read_csv(args.csv)

    counts = {}
    for _, row in df.iterrows():
        for c in split_cats(row["categories"]):
            counts[c] = counts.get(c, 0) + 1

    order = ["sexual", "violence", "shocking", "self-harm", "illegal activity", "harassment", "hate"]
    categories = []
    for c in order + [c for c in sorted(counts) if c not in order]:
        if c in counts and all(x["en"] != c for x in categories):
            categories.append({"code": c.replace(" ", "_"), "zh": CAT_ZH.get(c, c),
                               "en": c, "count": counts[c]})

    prompts = []
    for i, (_, row) in enumerate(df.iterrows(), start=1):
        cats = split_cats(row["categories"])
        prompts.append({
            "id": f"I2P-{i:04d}",
            "category": {"en": cats, "zh": [CAT_ZH.get(c, c) for c in cats]},
            "hard": as_int(row["hard"]),
            "prompt": {"en": as_str(row["prompt"]), "zh": None},
            "metrics": {
                "inappropriate_percentage": as_float(row["inappropriate_percentage"]),
                "nudity_percentage": as_float(row["nudity_percentage"]),
                "q16_percentage": as_float(row["q16_percentage"]),
                "sd_safety_percentage": as_float(row["sd_safety_percentage"]),
                "prompt_toxicity": as_float(row["prompt_toxicity"]),
            },
            "source": {"lexica_url": as_str(row["lexica_url"])},
            "sd_generation": {
                "seed": as_int(row["sd_seed"]),
                "guidance_scale": as_float(row["sd_guidance_scale"]),
                "image_width": as_int(row["sd_image_width"]),
                "image_height": as_int(row["sd_image_height"]),
                "model": as_str(row["sd_model"]),
            },
        })

    out = {
        "dataset": "i2p (Inappropriate Image Prompts)",
        "description": "I2P benchmark: 4703 real-world text-to-image prompts from lexica.art, "
                       "labeled across 7 unsafe concepts. For defensive/adversarial safety research only.",
        "taxonomy": "I2P 7 unsafe concepts (Schramowski et al., CVPR 2023): hate, harassment, "
                    "violence, self-harm, sexual, shocking, illegal activity",
        "source": {"name": "AIML-TUDA/i2p",
                   "url": "https://huggingface.co/datasets/AIML-TUDA/i2p",
                   "file": "i2p_benchmark.csv"},
        "note_on_counts": "categories[].count are per-label occurrences; multi-label, so they "
                          "sum to more than total.",
        "total": len(prompts),
        "categories": categories,
        "prompts": prompts,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"wrote {len(prompts)} prompts -> {args.out}")


if __name__ == "__main__":
    main()
