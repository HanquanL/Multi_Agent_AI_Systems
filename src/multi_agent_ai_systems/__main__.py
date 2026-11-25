"""Run the shared gateway as an MCP server: an LLM, a data source, or both.

uv run python -m multi_agent_ai_systems --http 8000 --llm-model llama3.2:3b         # LLM only
uv run --env-file .env python -m multi_agent_ai_systems --http 8000 --edgar          # SEC EDGAR
uv run --env-file .env python -m multi_agent_ai_systems --http 8000 --edgar --llm-model llama3.2:3b
Add --db / --llm-db to keep records / LLM answers in SQLite across restarts. Without --http, one
agent talks to its own server over stdio and nothing is shared.
"""

import argparse

from mcp.server.mcpserver import MCPServer

from multi_agent_ai_systems.allocation import QuotaFIFO
from multi_agent_ai_systems.cache import RecordCache
from multi_agent_ai_systems.config import llm_api_key, sec_user_agent
from multi_agent_ai_systems.connectors.base import RateLimit
from multi_agent_ai_systems.connectors.edgar import EdgarConnector
from multi_agent_ai_systems.connectors.edgar_service import (
    FactContext,
    FactService,
    register_tools,
)
from multi_agent_ai_systems.connectors.openai_compatible import OpenAICompatibleProvider
from multi_agent_ai_systems.gateway import Gateway
from multi_agent_ai_systems.inference import InferenceGateway
from multi_agent_ai_systems.llm import ModelPrice, ProviderLimits
from multi_agent_ai_systems.semantic import (
    LLMJudge,
    ReuseMode,
    RuleVerifier,
    SemanticReuse,
    VerifierChain,
)
from multi_agent_ai_systems.server import build_server
from multi_agent_ai_systems.store import SQLiteRecordStore
from multi_agent_ai_systems.validity import TTLPolicy

EDGAR_INSTRUCTIONS = """Shared gateway to SEC EDGAR. Call get_record with one of these keys:
  submissions/<CIK>                              a company's filing list
  companyfacts/<CIK>                             all XBRL financial facts for a company
  archive/edgar/data/<cik>/<accession>/<file>    one filing document
CIKs in keys are 10 digits, zero-padded: Apple is 0000320193.
Every result has a freshness label: source version, fetch time, age, and cache or source."""

LLM_INSTRUCTIONS = """Call generate to use the shared LLM. Identical requests from your workload
are answered once and reused while still valid; each result says whether it came from the cache,
the disk store, a merged call or the model. gateway_stats shows what was spent and avoided."""


def workload_quota(text: str) -> tuple[str, int]:
    """Parse NAME=QUOTA from the command line."""
    name, sep, quota = text.partition("=")
    if not sep or not name or not quota.isdigit() or int(quota) < 1:
        raise argparse.ArgumentTypeError(f"expected NAME=QUOTA with QUOTA >= 1, got {text!r}")
    return name, int(quota)

def workload_mode(text: str) -> tuple[str, ReuseMode]:
    """Parse NAME=MODE from the command line."""
    name, sep, mode = text.partition("=")
    choices = [m.value for m in ReuseMode]
    if not sep or not name or mode not in choices:
        raise argparse.ArgumentTypeError(
            f"expected NAME=MODE with MODE one of {', '.join(choices)}, got {text!r}"
        )
    return name, ReuseMode(mode)


def build_semantic(args: argparse.Namespace) -> SemanticReuse | None:
    if not args.semantic:
        return None
    limits = ProviderLimits(RateLimit(args.llm_rpm, 60.0))

    def provider(model: str) -> OpenAICompatibleProvider:
        return OpenAICompatibleProvider(
            args.llm_url, model, limits=limits, name=args.llm_provider, api_key=llm_api_key()
        )

    return SemanticReuse(
        provider(args.embed_model),
        VerifierChain(RuleVerifier(), LLMJudge(provider(args.judge_model))),
        dict(args.semantic),
        min_similarity=args.min_similarity,
        show_log=args.semantic_show_log,
        min_tokens=args.semantic_min_tokens,
    )

def build_inference(args: argparse.Namespace, facts: FactService | None) -> InferenceGateway | None:
    if not args.llm_model:
        return None
    tokens = RateLimit(args.llm_tpm, 60.0) if args.llm_tpm else None
    provider = OpenAICompatibleProvider(
        args.llm_url,
        args.llm_model,
        limits=ProviderLimits(RateLimit(args.llm_rpm, 60.0), tokens),
        name=args.llm_provider,
        api_key=llm_api_key(),
        version=args.llm_version,
    )
    return InferenceGateway(
        provider,
        cache=RecordCache(args.llm_cache_mb * 1_000_000),
        policy=TTLPolicy(args.llm_ttl),
        safety_factor=0.8,
        store=SQLiteRecordStore(args.llm_db, args.db_mb * 1_000_000) if args.llm_db else None,
        context_source=FactContext(facts) if facts is not None else None,
        price=ModelPrice(*args.llm_price) if args.llm_price else None,
        semantic=build_semantic(args),
    )

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Shared gateway for AI applications")
    parser.add_argument(
        "--edgar", action="store_true", help="serve SEC EDGAR data and get_fact (needs .env)"
    )
    parser.add_argument("--http", type=int, metavar="PORT", help="serve HTTP on this port")
    parser.add_argument("--cache-mb", type=int, default=256, help="cache capacity in MB")
    parser.add_argument("--ttl", type=float, default=3600.0, help="cache TTL in seconds")
    parser.add_argument(
        "--db", metavar="PATH", help="keep fetched records in this SQLite file across restarts"
    )
    parser.add_argument("--db-mb", type=int, default=1024, help="disk store capacity in MB")
    parser.add_argument(
        "--workload",
        type=workload_quota,
        action="append",
        default=[],
        metavar="NAME=QUOTA",
        help="give a workload at most QUOTA upstream requests per second while others wait"
        " (repeatable)",
    )
    parser.add_argument(
        "--default-quota",
        type=int,
        metavar="N",
        help="quota shared by all workloads not listed with --workload",
    )
    parser.add_argument("--llm-model", help="enable the generate tool with this model")
    parser.add_argument(
        "--llm-url", default="http://localhost:11434/v1", help="OpenAI-compatible API base URL"
    )
    parser.add_argument("--llm-provider", default="ollama", help="provider name, part of the key")
    parser.add_argument("--llm-version", help="pin the model version instead of looking it up")
    parser.add_argument("--llm-rpm", type=int, default=60, help="provider requests per minute")
    parser.add_argument("--llm-tpm", type=int, help="provider tokens per minute, if limited")
    parser.add_argument("--llm-cache-mb", type=int, default=64, help="LLM cache capacity in MB")
    parser.add_argument(
        "--llm-ttl", type=float, default=604800.0, help="LLM response lifetime in seconds"
    )
    parser.add_argument("--llm-db", metavar="PATH", help="keep LLM responses in this SQLite file")
    parser.add_argument(
        "--llm-price",
        type=float,
        nargs=2,
        metavar=("INPUT", "OUTPUT"),
        help="published USD per million tokens, for estimates",
    )
    parser.add_argument(
        "--semantic",
        type=workload_mode,
        action="append",
        default=[],
        metavar="NAME=MODE",
        help="reuse answers to differently worded questions for a workload: exact (default), "
        "shadow (log only) or active (repeatable)",
    )
    parser.add_argument(
        "--embed-model", default="nomic-embed-text:latest", help="embedding model for --semantic"
    )
    parser.add_argument(
        "--judge-model", default="qwen2.5:7b", help="model that judges question equivalence"
    )
    parser.add_argument(
        "--min-similarity", type=float, default=0.9, help="similarity needed to be a candidate"
    )
    parser.add_argument(
        "--semantic-min-tokens",
        type=int,
        default=0,
        help="only look for similar questions when input plus " \
        "max output is at least this many tokens",
    )
    parser.add_argument(
        "--semantic-show-log",
        action="store_true",
        help="show recent questions and decisions in gateway_stats (any client can read them)",
    )
    return parser.parse_args(argv)


def build(args: argparse.Namespace) -> MCPServer:
    if not args.edgar and not args.llm_model:
        raise SystemExit("nothing to serve: add --llm-model, --edgar, or both")
    if args.semantic and not args.llm_model:
        raise SystemExit("--semantic needs --llm-model")
    gateway = facts = None
    tools = []
    answers = {}
    instructions = []
    if args.edgar:
        gateway = Gateway(
            EdgarConnector(sec_user_agent()),
            cache=RecordCache(args.cache_mb * 1_000_000),
            policy=TTLPolicy(args.ttl),
            safety_factor=0.8,
            allocation=QuotaFIFO(dict(args.workload), default_quota=args.default_quota),
            store=SQLiteRecordStore(args.db, args.db_mb * 1_000_000) if args.db else None,
        )
        facts = FactService(gateway)
        tools.append(lambda s: register_tools(s, facts))
        answers["get_fact"] = facts.answers
        instructions.append(EDGAR_INSTRUCTIONS)
    if args.llm_model:
        instructions.append(LLM_INSTRUCTIONS)
    return build_server(
        gateway,
        name="shared-gateway",
        instructions="\n\n".join(instructions),
        tools=tools,
        answers=answers,
        inference=build_inference(args, facts),
    )


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    server = build(args)
    if args.http:
        server.run("streamable-http", port=args.http)
    else:
        server.run()


if __name__ == "__main__":
    main()
