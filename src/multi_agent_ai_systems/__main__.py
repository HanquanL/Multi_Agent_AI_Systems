"""Run the shared data gateway for SEC EDGAR as an MCP server.

uv run --env-file .env python -m multi_agent_ai_systems --http 8000   # shared, many agents
uv run --env-file .env python -m multi_agent_ai_systems --http 8000 --db gateway.db  # + disk
uv run --env-file .env python -m multi_agent_ai_systems               # stdio, one agent
Every result has a freshness label: source version, fetch time, age, and cache or source.
For one financial value with its source filing, call get_fact (e.g. NetIncome, FY2024).

uv run --env-file .env python -m multi_agent_ai_systems --http 8000 --llm-model llama3.2:3b  # + LLM
"""

import argparse

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
from multi_agent_ai_systems.server import build_server
from multi_agent_ai_systems.store import SQLiteRecordStore
from multi_agent_ai_systems.validity import TTLPolicy

INSTRUCTIONS = """Shared gateway to SEC EDGAR. Call get_record with one of these keys:
  submissions/<CIK>                              a company's filing list
  companyfacts/<CIK>                             all XBRL financial facts for a company
  archive/edgar/data/<cik>/<accession>/<file>    one filing document
CIKs in keys are 10 digits, zero-padded: Apple is 0000320193.
Every result has a freshness label: source version, fetch time, age, and cache or source."""


def workload_quota(text: str) -> tuple[str, int]:
    """Parse NAME=QUOTA from the command line."""
    name, sep, quota = text.partition("=")
    if not sep or not name or not quota.isdigit() or int(quota) < 1:
        raise argparse.ArgumentTypeError(f"expected NAME=QUOTA with QUOTA >= 1, got {text!r}")
    return name, int(quota)

def build_inference(args: argparse.Namespace, facts: FactService) -> InferenceGateway | None:
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
        context_source=FactContext(facts),
        price=ModelPrice(*args.llm_price) if args.llm_price else None,
    )

def main() -> None:
    parser = argparse.ArgumentParser(description="Shared data gateway for SEC EDGAR")
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
    args = parser.parse_args()

    connector = EdgarConnector(sec_user_agent())
    gateway = Gateway(
        connector,
        cache=RecordCache(args.cache_mb * 1_000_000),
        policy=TTLPolicy(args.ttl),
        safety_factor=0.8,
        allocation=QuotaFIFO(dict(args.workload), default_quota=args.default_quota),
        store=SQLiteRecordStore(args.db, args.db_mb * 1_000_000) if args.db else None,
    )
    facts = FactService(gateway)
    server = build_server(
        gateway,
        instructions=INSTRUCTIONS,
        tools=[lambda s: register_tools(s, facts)],
        answers={"get_fact": facts.answers},
        inference=build_inference(args, facts),
    )
    if args.http:
        server.run("streamable-http", port=args.http)
    else:
        server.run()


if __name__ == "__main__":
    main()
