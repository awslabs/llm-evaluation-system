#!/usr/bin/env python3
"""Streaming time-to-first-token (TTFT) probe for a Bedrock/Mantle model vs an
OpenAI model.

Why this is separate from compare_models.py: the aiwf benchmark does NOT stream,
so its latency column is total generate time (an upper bound on TTFT), not the
first-token latency a user actually feels. This probe streams both providers and
times the first content token, so it measures TTFT directly.

Honest scope: the two models are reached over DIFFERENT network paths (an OpenAI
model hits OpenAI's public API; a Mantle model hits the AWS endpoint), so this is
a PLATFORM comparison ("provider X as I would call it"), not a model-isolated one.
The ranking is trustworthy; don't quote the absolute seconds as the model's
intrinsic latency.

Auth: OPENAI_API_KEY in the env for the OpenAI model; ambient AWS creds for the
Mantle model (a bearer token is minted per run).

Example:
  OPENAI_API_KEY=sk-... AWS_REGION=us-east-2 python ttft_probe.py \
    --mantle-model openai.gpt-5.6-luna --openai-model gpt-4o-mini --trials 12
"""
import argparse
import os
import statistics
import time

PROMPT = "In two sentences, explain what a knowledge base is for a conference assistant."


def _pct(xs, p):
    xs = sorted(xs)
    k = min(len(xs) - 1, int(round((p / 100) * (len(xs) - 1))))
    return xs[k]


def _summarize(name, ttfts, totals):
    print(f"\n=== {name} ===  (n={len(ttfts)})")
    print(f"  TTFT  p50 {statistics.median(ttfts):.3f}s  p95 {_pct(ttfts, 95):.3f}s"
          f"  min {min(ttfts):.3f}  max {max(ttfts):.3f}  mean {statistics.mean(ttfts):.3f}")
    print(f"  TOTAL p50 {statistics.median(totals):.3f}s  p95 {_pct(totals, 95):.3f}s"
          f"  mean {statistics.mean(totals):.3f}")


def probe_openai(model, trials):
    from openai import OpenAI

    client = OpenAI()
    ttfts, totals = [], []
    for _ in range(trials):
        t0 = time.perf_counter()
        first = None
        stream = client.chat.completions.create(
            model=model, messages=[{"role": "user", "content": PROMPT}], stream=True,
        )
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content and first is None:
                first = time.perf_counter()
        end = time.perf_counter()
        if first:
            ttfts.append(first - t0)
            totals.append(end - t0)
    return ttfts, totals


def probe_mantle(model, trials, region):
    from aws_bedrock_token_generator import provide_token
    from openai import OpenAI

    client = OpenAI(
        base_url=f"https://bedrock-mantle.{region}.api.aws/openai/v1",
        api_key=provide_token(region=region),
    )
    ttfts, totals = [], []
    for _ in range(trials):
        t0 = time.perf_counter()
        first = None
        stream = client.responses.create(model=model, input=PROMPT, stream=True)
        for event in stream:
            et = getattr(event, "type", "")
            if et.endswith(".delta") and first is None:
                first = time.perf_counter()
        end = time.perf_counter()
        if first:
            ttfts.append(first - t0)
            totals.append(end - t0)
    return ttfts, totals


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mantle-model", default="openai.gpt-5.6-luna")
    ap.add_argument("--openai-model", default="gpt-4o-mini")
    ap.add_argument("--trials", type=int, default=12)
    args = ap.parse_args()
    region = os.environ.get("AWS_REGION", "us-east-2")

    print(f"region={region}  trials={args.trials}")
    lt, lo = probe_mantle(args.mantle_model, args.trials, region)
    _summarize(f"{args.mantle_model} (Bedrock Mantle, streaming)", lt, lo)
    mt, mo = probe_openai(args.openai_model, args.trials)
    _summarize(f"{args.openai_model} (OpenAI public API, streaming)", mt, mo)


if __name__ == "__main__":
    main()
