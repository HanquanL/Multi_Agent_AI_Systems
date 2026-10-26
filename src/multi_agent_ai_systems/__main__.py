"""Run the shared data gateway for SEC EDGAR as an MCP server.

uv run --env-file .env python -m multi_agent_ai_systems --http 8000   # shared, many agents
uv run --env-file .env python -m multi_agent_ai_systems               # stdio, one agent
"""

import argparse

from multi_agent_ai_systems.cache import RecordCache
from multi_agent_ai_systems.config import sec_user_agent
from multi_agent_ai_systems.connectors.edgar import EdgarConnector
from multi_agent_ai_systems.gateway import Gateway
from multi_agent_ai_systems.server import build_server
from multi_agent_ai_systems.validity import TTLPolicy

INSTRUCTIONS = """Shared gateway to SEC EDGAR. Call get_record with one of these keys:
  submissions/<CIK>                              a company's filing list
  companyfacts/<CIK>                             all XBRL financial facts for a company
  archive/edgar/data/<cik>/<accession>/<file>    one filing document
CIKs in keys are 10 digits, zero-padded: Apple is 0000320193.
Every result has a freshness label: source version, fetch time, age, and cache or source."""


def main() -> None:
    parser = argparse.ArgumentParser(description="Shared data gateway for SEC EDGAR")
    parser.add_argument("--http", type=int, metavar="PORT", help="serve HTTP on this port")
    parser.add_argument("--cache-mb", type=int, default=256, help="cache capacity in MB")
    parser.add_argument("--ttl", type=float, default=3600.0, help="cache TTL in seconds")
    args = parser.parse_args()

    connector = EdgarConnector(sec_user_agent())
    gateway = Gateway(
        connector,
        cache=RecordCache(args.cache_mb * 1_000_000),
        policy=TTLPolicy(args.ttl),
        safety_factor=0.8,
    )
    server = build_server(gateway, instructions=INSTRUCTIONS)
    if args.http:
        server.run("streamable-http", port=args.http)
    else:
        server.run()


if __name__ == "__main__":
    main()
