"""Demo actions. Each one uses the gateway only through its MCP tools, the way any
application would, and reports what changed in gateway_stats during that action.

Start the gateway first (terminal 1):
  uv run --env-file .env python -m multi_agent_ai_systems --http 8000 --edgar \
      --llm-model llama3.2:3b --db demo-data.db --llm-db demo-llm.db
      --semantic semantic-team=active --semantic warm-up=shadow 
      --semantic semantic-team=active --semantic-show-log --min-similarity 0.8
Then run the steps in a terminal (the fallback if the dashboard fails):
  uv run python demo/actions.py
"""

import asyncio
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from mcp import Client

URL = "http://127.0.0.1:8000/mcp"
TEAM = "support-team"
AGENTS = ("agent-1", "agent-2", "agent-3")
QUESTIONS = [
    "Name the largest planet in the solar system. Answer with one word.",
    "Translate 'thank you' into Spanish. Answer with the translation only.",
    "At sea level, what is the boiling point of water in Celsius? Answer with a number.",
    "In one sentence, what does a network router do?",
    "Write a haiku about waiting in line.",
    "In one sentence, why do web browsers cache pages?",
]
COMPANY = ("320193", "Apple")
METRICS = {
    "NetIncome": "net income",
    "Revenue": "revenue",
    "OperatingIncome": "operating income",
    "EPSDiluted": "diluted earnings per share",
    "TotalAssets": "total assets",
    "CashAndEquivalents": "cash and cash equivalents",
}
PERIODS = ["FY2024", "FY2025", "2024Q2", "latest_annual"]
SEMANTIC_TEAM = "semantic-team"
SIMILAR_CASES = {
    "Same meaning": ("What is the largest planet?", "Which planet is the biggest?"),
    "Rules reject (can vs should)": ("Can this file be deleted?", "Should this file be deleted?"),
    "Judge rejects (different detail)": (
        "Explain TCP in one sentence.",
        "Explain TCP in detail.",
    ),
    "Judge unsure (different play)": ("Who wrote Hamlet?", "Who wrote Macbeth?"),
}
WARM_UP_TEAM = "warm-up"


def period_text(period: str) -> str:
    if period == "latest_annual":
        return "its latest fiscal year"
    if period.startswith("FY"):
        return f"fiscal year {period[2:]}"
    return f"fiscal {period[4:]} {period[:4]}"  # 2024Q2 -> fiscal Q2 2024


def fact_question(metric: str, period: str) -> str:
    return f"{COMPANY[1]} {METRICS[metric]}, {period_text(period)}"


def grounded_question(metric: str, period: str) -> str:
    cik, name = COMPANY
    return (
        f"The figures given are for {name} Inc., whose SEC CIK is {cik}. "
        f"What was {name}'s {METRICS[metric]} for {period_text(period)}? "
        "Answer in one sentence."
    )



Row = dict[str, Any]


@dataclass
class ActionResult:
    title: str
    rows: list[Row]  # one per request
    changes: dict[str, int]  # how much each gateway counter moved during the action
    seconds: float  # elapsed wall-clock time of the whole action
    question: str = ""  # what was asked, shown above the results


def snapshot(stats: dict[str, Any]) -> dict[str, int]:
    """The counters the demo talks about, summed over all workloads."""
    semantic = (stats.get("inference") or {}).get("semantic") or {}
    llm = Counter()
    for counts in ((stats.get("inference") or {}).get("workloads") or {}).values():
        llm.update({name: value for name, value in counts.items() if isinstance(value, int)})
    return {
        "model calls": llm["calls"],
        "merged into a call in flight": llm["merged"],
        "answers reused": llm["cache_hits"],
        "of which from disk": llm["store_hits"],
        "similar answers reused": llm["semantic_hits"],
        "judge calls": (semantic.get("judge_cost") or {}).get("calls", 0),
        "tokens spent": llm["prompt_tokens_spent"] + llm["completion_tokens_spent"],
        "tokens avoided": llm["prompt_tokens_avoided"] + llm["completion_tokens_avoided"],
        "SEC requests": stats.get("upstream_attempts", 0),
        "records reused": stats.get("hits", 0),
    }


async def stats(client: Client) -> dict[str, Any]:
    return (await client.call_tool("gateway_stats", {})).structured_content


async def measured(title: str, work: Callable[[Client], Awaitable[list[Row]]], question: 
                   str = "") -> ActionResult:
    async with Client(URL) as client:
        before = snapshot(await stats(client))
        start = time.perf_counter()
        rows = await work(client)
        seconds = time.perf_counter() - start
        after = snapshot(await stats(client))
    changes = {name: after[name] - before[name] for name in after}
    return ActionResult(title, rows, changes, round(seconds, 2), question)


async def generate(
    client: Client,
    prompt: str,
    *,
    workload: str = TEAM,
    agent: str = "agent-1",
    context: list[str] | None = None,
    max_tokens: int = 120,
) -> Row:
    args = {
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "workload": workload,
        "agent_id": agent,
    }
    if context:
        args["context"] = context
    start = time.perf_counter()
    result = await client.call_tool("generate", args)
    seconds = round(time.perf_counter() - start, 2)
    row = {"workload": workload, "agent": agent, "seconds": seconds}
    if result.is_error:
        return {**row, "source": "error", "answer": result.content[0].text}
    answer = result.structured_content
    row = {
        **row,
        "source": answer["source"],
        "age (s)": round(answer["age_seconds"]),
        "answer": answer["text"],
    }
    if answer.get("similar_to"):
        row["similar to"] = answer["similar_to"]
        row["similarity"] = answer["similarity"]
    return row


async def concurrent(question: str) -> ActionResult:
    async def work(client: Client) -> list[Row]:
        calls = [generate(client, question, agent=agent) for agent in AGENTS]
        return list(await asyncio.gather(*calls))

    return await measured("Three agents ask the same question at the same time", work, question)


async def repeat(question: str) -> ActionResult:
    async def work(client: Client) -> list[Row]:
        calls = [generate(client, question, agent=agent) for agent in AGENTS]
        return list(await asyncio.gather(*calls))

    return await measured("The same three agents ask again", work, question)


async def other_team(question: str) -> ActionResult:
    async def work(client: Client) -> list[Row]:
        return [await generate(client, question, workload="team-b")]

    return await measured("Another team asks the same question", work,question)

APPLE_FILINGS = "submissions/0000320193"


async def fetch_record(client: Client, key: str, agent: str) -> Row:
    args = {"key": key, "workload": TEAM, "agent_id": agent}
    start = time.perf_counter()
    result = await client.call_tool("get_record", args)
    row = {"agent": agent, "seconds": round(time.perf_counter() - start, 2)}
    if result.is_error:
        return {**row, "source": "error", "detail": result.content[0].text}
    label = result.structured_content["freshness"]
    return {
        **row,
        "source": label["source"],
        "version": label["version"],
        "age (s)": round(label["age_seconds"]),
    }


async def shared_record() -> ActionResult:
    async def work(client: Client) -> list[Row]:
        calls = [fetch_record(client, APPLE_FILINGS, agent) for agent in AGENTS]
        return list(await asyncio.gather(*calls))

    question = f"get_record {APPLE_FILINGS} (Apple's list of SEC filings)"
    return await measured("Three agents fetch the same SEC record at the same time", 
                          work, question)

async def data_path(metrics: list[str], period: str) -> ActionResult:
    asked = [*metrics, metrics[0]]  # the first one again, to show the answer being reused

    async def work(client: Client) -> list[Row]:
        rows = []
        for metric in asked:
            args = {"cik": COMPANY[0], "metric": metric, "period": period}
            result = await client.call_tool("get_fact", args)
            question = fact_question(metric, period)
            if result.is_error:
                rows.append({"question": question, "result": result.content[0].text})
                continue
            fact = result.structured_content
            rows.append(
                {
                    "question": question,
                    "value": f"{fact['value']:,} {fact['unit']}",
                    "answer cache": fact["cache"],
                    "source filing": f"{fact['form']} filed {fact['filed']}",
                }
            )
        return rows

    question = "; ".join(fact_question(m, period) for m in asked)
    return await measured("Financial facts through the EDGAR plugin", work, question)


async def grounded(metric: str = "NetIncome", period: str = "FY2024") -> ActionResult:
    question = grounded_question(metric, period)
    context = [f"{COMPANY[0]}:{metric}:{period}"]

    async def work(client: Client) -> list[Row]:
        first = await generate(client, question, context=context, max_tokens=200)
        second = await generate(client, question, context=context, max_tokens=200)
        return [first, second]

    return await measured("An LLM answer grounded in a checked fact", work, question)

async def last_decision(client: Client, question: str) -> Row:
    """The gateway's semantic decision for this question; needs --semantic-show-log."""
    semantic = ((await stats(client)).get("inference") or {}).get("semantic") or {}
    for entry in reversed(semantic.get("recent") or []):
        if entry["workload"] == SEMANTIC_TEAM and entry["question"] == question:
            return {
                "decision": entry["decision"],
                "judged by": entry.get("by", "-"),
                "reason": entry.get("reason", "-"),
            }
    return {"decision": "not looked up (an exact hit, or the log is off)"}


async def similar(case: str) -> ActionResult:
    earlier, question = SIMILAR_CASES[case]

    async def work(client: Client) -> list[Row]:
        rows = []
        for text in (earlier, question):
            row = await generate(client, text, workload=SEMANTIC_TEAM, max_tokens=300)
            rows.append({"question": text, **row})
        rows[-1].update(await last_decision(client, question))
        return rows

    return await measured(f"Similar question: {case}", work, f"{earlier}  ->  {question}")

async def tool_names() -> set[str]:
    async with Client(URL) as client:
        return {tool.name for tool in (await client.list_tools()).tools}

async def current_totals() -> dict[str, int]:
    async with Client(URL) as client:
        return snapshot(await stats(client))

async def semantic_modes() -> dict[str, str]:
    async with Client(URL) as client:
        semantic = ((await stats(client)).get("inference") or {}).get("semantic") or {}
    return semantic.get("modes") or {}

async def warm_up() -> Row:
    """Load every model the demo uses before the audience is watching: the answer model, and,
    through a shadow-mode workload, the embedding model and the judge. Raises if the answer
    model did not answer, so a failed warm-up is never reported as success."""
    async with Client(URL) as client:
        row = await generate(client, "Say OK.", workload=WARM_UP_TEAM, max_tokens=5)
        await generate(client, "Please say OK.", workload=WARM_UP_TEAM, max_tokens=5)
    if row["source"] == "error":
        raise RuntimeError(row["answer"])
    return row
    
def show(result: ActionResult) -> None:
    print(f"\n== {result.title}  ({result.seconds} s)")
    if result.question:
        print(f"  question: {result.question}")
    for row in result.rows:
        print("  " + "  ".join(f"{key}: {value}" for key, value in row.items()))
    moved = {name: change for name, change in result.changes.items() if change}
    print("  changes: " + (", ".join(f"{n} +{c}" for n, c in moved.items()) or "none"))


def main() -> None:
    names = asyncio.run(tool_names())
    print("gateway tools:", ", ".join(sorted(names)))
    if "get_fact" not in names:
        print("warning: this gateway was started without --edgar; the data steps will fail")
    print("warming up the model...")
    asyncio.run(warm_up())
    question = QUESTIONS[0]
    steps = [
        lambda: concurrent(question),
        lambda: repeat(question),
        lambda: other_team(question),
        shared_record,
        lambda: data_path(["NetIncome", "Revenue"], "FY2024"),
        grounded,
        lambda: similar("Same meaning"),
    ]
    for step in steps:
        input("\nPress Enter for the next step...")
        show(asyncio.run(step()))
    print("\nTo show persistence: restart the gateway without deleting the databases, then run")
    print("this again; the answers come back from disk.")


if __name__ == "__main__":
    main()