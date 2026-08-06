#!/usr/bin/env python3
"""Compare two target models on the aiwf benchmark, repeated N times.

Runs both models through the SAME 30-turn conversation each repeat (so they
face identical inputs) and scores them with a fixed judge or jury. Reports
per-model pass_rate mean/std, per-dimension breakdown, the benchmark's latency
column, and a paired t-test on the per-repeat difference.

This is a thin driver over the shipped `run_multiturn_benchmark` handler — it
adds nothing to the measurement, it just repeats it and does the arithmetic.
For a quality comparison of two *target* models, hold the judge fixed (default:
a single Opus 5 judge); pass --jury to use a majority-vote panel instead (the
scores are then not comparable to single-judge runs — see the benchmark's
eval.yaml caveats).

Auth:
  - Bedrock / Mantle models (bedrock/*, openai/bedrock/*): ambient AWS creds.
  - OpenAI models (openai/gpt-*): set OPENAI_API_KEY in the environment. The
    repo also reads it from ~/.eval-mcp/.env.keys; this script only reads the
    env var, so a temporary key can be passed without persisting it.

Example:
  OPENAI_API_KEY=sk-... AWS_REGION=us-east-2 \
    python compare_models.py \
      --models openai/bedrock/gpt-5.6-luna openai/gpt-4o-mini \
      --repeats 10 --judge bedrock/us.anthropic.claude-opus-5
"""
import argparse
import asyncio
import glob
import json
import math
import os
import statistics
from collections import defaultdict

DIMS = ("tool_use_correct", "instruction_following", "kb_grounding")


def _paired_t_pvalue(diffs):
    """Two-sided paired t-test p-value via the Student-t tail integral.

    Kept dependency-free (no scipy): integrates the t density directly, which
    is the definition of the p-value and needs no special-function library.
    Assumes the differences are ~Normal; at small n prefer this as a guide, not
    gospel (a Wilcoxon signed-rank test assumes only symmetry).
    """
    n = len(diffs)
    if n < 2:
        return None
    mean = statistics.mean(diffs)
    sd = statistics.stdev(diffs)
    if sd == 0:
        return {"mean": mean, "sd": 0.0, "t": math.inf, "df": n - 1, "p": 0.0}
    se = sd / math.sqrt(n)
    t = mean / se
    nu = n - 1

    def density(x):
        c = math.gamma((nu + 1) / 2) / (math.sqrt(nu * math.pi) * math.gamma(nu / 2))
        return c * (1 + x * x / nu) ** (-(nu + 1) / 2)

    # two-sided p = 2 * integral_{|t|}^inf f(x) dx, Simpson's rule
    lo, hi, steps = abs(t), abs(t) + 80, 200_000
    h = (hi - lo) / steps
    s = density(lo) + density(hi)
    for i in range(1, steps):
        s += (4 if i % 2 else 2) * density(lo + i * h)
    p = 2 * s * h / 3
    return {"mean": mean, "sd": sd, "se": se, "t": t, "df": nu, "p": min(1.0, p)}


async def _run_one(rep, models, judge, jury, user_prefix, region):
    from eval_mcp.tools.multiturn_benchmarks import handle_run_multiturn_benchmark

    args = {
        "task": "aiwf_medium_context",
        "user_id": f"{user_prefix}-rep{rep}",
        "providers": models,
    }
    if jury:
        args["judge_models"] = judge if isinstance(judge, list) else [judge]
    else:
        args["judge_model"] = judge[0] if isinstance(judge, list) else judge
    out = await handle_run_multiturn_benchmark(args)
    res = json.loads(out[0].text)
    print(f"  rep{rep}: success={res.get('success')} {res.get('error', '')[:120]}", flush=True)
    return res


def _read_results(user_prefix):
    """Pull per-model, per-rep metrics from the eval logs on disk."""
    from inspect_ai.log import read_eval_log  # sync read is fine post-hoc

    bases = [".smoke/users", os.path.expanduser("~/.eval-mcp/users"), "backend/users"]
    data = defaultdict(dict)
    for base in bases:
        for rep_dir in sorted(glob.glob(f"{base}/{user_prefix}-rep*/logs")):
            rep = int(rep_dir.split(f"{user_prefix}-rep")[1].split("/")[0])
            for path in sorted(glob.glob(rep_dir + "/*.eval"), key=os.path.getmtime):
                log = read_eval_log(path)
                model = log.eval.model.split("/")[-1]
                for s in (log.samples or []):
                    sc = s.scores.get("aiwf_turn_judge")
                    if not sc:
                        continue
                    md = sc.metadata
                    data[model][rep] = {
                        "pass_rate": sc.value,
                        **{d: md.get(d) for d in DIMS},
                        "median_latency": md.get("median_response_seconds"),
                    }
    return data


def _report(data):
    models = sorted(data)
    for m in models:
        reps = sorted(data[m])
        vals = [data[m][r]["pass_rate"] for r in reps]
        print(f"\n=== {m} ===  (n={len(reps)})")
        print(f"  pass_rate: {[f'{v:.3f}' for v in vals]}")
        if len(vals) > 1:
            print(f"  mean {statistics.mean(vals):.3f}  std {statistics.stdev(vals):.3f}"
                  f"  min {min(vals):.3f}  max {max(vals):.3f}")
        for d in DIMS:
            xs = [data[m][r][d] for r in reps if data[m][r][d] is not None]
            if xs:
                print(f"  {d}: mean {statistics.mean(xs):.1f}/30")
        lats = [data[m][r]["median_latency"] for r in reps if data[m][r]["median_latency"]]
        if lats:
            print(f"  median latency: mean {statistics.mean(lats):.2f}s")

    if len(models) == 2:
        reps = sorted(set(data[models[0]]) & set(data[models[1]]))
        diffs = [data[models[0]][r]["pass_rate"] - data[models[1]][r]["pass_rate"] for r in reps]
        stat = _paired_t_pvalue(diffs)
        print(f"\npaired diff ({models[0]} - {models[1]}): {[f'{d:+.3f}' for d in diffs]}")
        if stat:
            print(f"  mean {stat['mean']:+.3f}  t={stat['t']:.2f}  df={stat['df']}"
                  f"  two-sided p={stat['p']:.4f}")


async def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--models", nargs=2, required=True, help="two model ids to compare")
    ap.add_argument("--repeats", type=int, default=10)
    ap.add_argument("--judge", nargs="+", default=["bedrock/us.anthropic.claude-opus-5"],
                    help="one judge (default) or several with --jury")
    ap.add_argument("--jury", action="store_true", help="majority-vote panel over --judge models")
    ap.add_argument("--user-prefix", default="cmp")
    args = ap.parse_args()

    region = os.environ.get("AWS_REGION", "us-east-2")
    os.environ.setdefault("AWS_REGION", region)
    print(f"region={region}  repeats={args.repeats}  "
          f"{'jury' if args.jury else 'single judge'}={args.judge}")

    results = await asyncio.gather(*(
        _run_one(r, args.models, args.judge, args.jury, args.user_prefix, region)
        for r in range(1, args.repeats + 1)
    ))
    ok = sum(1 for r in results if r.get("success"))
    print(f"\n{ok}/{args.repeats} runs succeeded")
    _report(_read_results(args.user_prefix))


if __name__ == "__main__":
    asyncio.run(main())
