"""How well do candidate retrieval and verification separate questions whose answer can be
reused from questions whose answer cannot? Runs a labelled set of question pairs against the
models directly, not through the gateway. The truth is each pair's direct_answer_reuse label.

  uv run python bench/judge_eval.py --judge-model qwen2.5:7b

Prints a summary and writes one line per pair to bench/results/.
"""

import argparse
import asyncio
import json
import math
import time
from collections import Counter
from datetime import date
from pathlib import Path

from multi_agent_ai_systems.connectors.base import RateLimit
from multi_agent_ai_systems.connectors.openai_compatible import OpenAICompatibleProvider
from multi_agent_ai_systems.llm import ProviderLimits
from multi_agent_ai_systems.semantic import LLMJudge, RuleVerifier, Verdict

HERE = Path(__file__).parent
FLOORS = (0.8, 0.85, 0.9, 0.95)
VERDICTS = ("same", "different", "unsure")


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / math.sqrt(sum(x * x for x in a) * sum(y * y for y in b))


async def evaluate(pairs, url, judge_model, embed_model):
    limits = ProviderLimits(RateLimit(600, 60.0))
    llm = OpenAICompatibleProvider(url, judge_model, limits=limits)
    embedder = OpenAICompatibleProvider(url, embed_model, limits=limits)
    judge = LLMJudge(llm)
    rules = RuleVerifier()
    results = []
    try:
        for pair in pairs:
            similarity = cosine(await embedder.embed(pair["a"]), await embedder.embed(pair["b"]))
            rule = await rules.verify(pair["b"], pair["a"])
            start = time.perf_counter()
            judged = await judge.verify(pair["b"], pair["a"])  # always, to measure it alone
            seconds = time.perf_counter() - start
            chain = rule if rule.verdict is Verdict.DIFFERENT else judged
            results.append(
                {
                    **pair,
                    "similarity": round(similarity, 4),
                    "rules": rule.verdict.value,
                    "rule_reason": rule.reason,
                    "judge": judged.verdict.value,
                    "judge_seconds": round(seconds, 3),
                    "chain": chain.verdict.value,
                }
            )
            truth = "reuse" if pair["direct_answer_reuse"] else "no"
            print(
                f"{pair['id']:<4} {truth:<5} sim {similarity:.3f}  "
                f"rules {rule.verdict.value:<9} judge {judged.verdict.value:<9} "
                f"-> {chain.verdict.value}"
            )
    finally:
        await llm.aclose()
        await embedder.aclose()
    return results, judge


def summarize(results, judge):
    reusable = [r for r in results if r["direct_answer_reuse"]]
    not_reusable = [r for r in results if not r["direct_answer_reuse"]]
    for title, field in (("judge alone", "judge"), ("rules, then judge", "chain")):
        print(f"\n{title}:")
        for name, group in (("reusable", reusable), ("not reusable", not_reusable)):
            counts = Counter(r[field] for r in group)
            spread = "  ".join(f"{v} {counts[v]}" for v in VERDICTS)
            print(f"  {name:<12} n={len(group):<3} judged: {spread}")

    print("\nreuse = similarity >= floor, and rules-then-judge says same")
    for floor in FLOORS:
        right = sum(r["similarity"] >= floor and r["chain"] == "same" for r in reusable)
        wrong = sum(r["similarity"] >= floor and r["chain"] == "same" for r in not_reusable)
        reached = sum(r["similarity"] >= floor for r in not_reusable)
        precision = f"{right / (right + wrong):.2f}" if right + wrong else "n/a"
        print(
            f"  floor {floor:.2f}: coverage {right}/{len(reusable)}, "
            f"false reuse {wrong}/{len(not_reusable)}, precision {precision}, "
            f"non-reusable pairs reaching the verifiers {reached}/{len(not_reusable)}"
        )

    seconds = sorted(r["judge_seconds"] for r in results)
    print(
        f"\njudge: {judge.calls} calls, {judge.prompt_tokens / judge.calls:.0f} prompt tokens "
        f"per call, median {seconds[len(seconds) // 2]:.2f} s, slowest {seconds[-1]:.2f} s"
    )
    for r in not_reusable:
        if r["chain"] == "same":
            print(
                f"FALSE SAME {r['id']} ({r['relation']}, {r['kind']}, sim {r['similarity']}): "
                f"{r['a']!r} / {r['b']!r}"
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pairs", type=Path, default=HERE / "pairs.jsonl")
    parser.add_argument("--url", default="http://localhost:11434/v1")
    parser.add_argument("--judge-model", default="qwen2.5:7b")
    parser.add_argument("--embed-model", default="nomic-embed-text:latest")
    args = parser.parse_args()

    lines = args.pairs.read_text(encoding="utf-8").splitlines()
    pairs = [json.loads(line) for line in lines if line.strip()]
    results, judge = asyncio.run(evaluate(pairs, args.url, args.judge_model, args.embed_model))
    summarize(results, judge)

    out = HERE / "results" / f"judge-{args.judge_model.replace(':', '_')}-{date.today()}.jsonl"
    out.parent.mkdir(exist_ok=True)
    out.write_text("".join(json.dumps(r) + "\n" for r in results), encoding="utf-8")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()