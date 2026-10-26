"""Live end-to-end demo: three MCP clients share one gateway over HTTP.

Start the server first, in another terminal:
    uv run --env-file .env python -m multi_agent_ai_systems --http 8000
Then run:
    uv run python scripts/demo_three_clients.py
Restart the server before each run for clean numbers; the cache lives as long as the server.
"""

import asyncio
import json
import sys
from typing import Any

from mcp import Client

URL = "http://127.0.0.1:8000/mcp"
KEY = "submissions/0000320193"  # Apple's filing list


async def ask(name: str) -> dict[str, Any]:
    async with Client(URL) as client:
        result = await client.call_tool("get_record", {"key": KEY})
    if result.is_error:
        sys.exit(f"{name}: {result.content[0].text}")
    return result.structured_content["freshness"]


async def stats() -> dict[str, Any]:
    async with Client(URL) as client:
        return (await client.call_tool("gateway_stats")).structured_content


async def main() -> None:
    for round_no in (1, 2):
        labels = await asyncio.gather(*(ask(f"agent-{i}") for i in range(1, 4)))
        print(f"round {round_no}:")
        for i, label in enumerate(labels, 1):
            print(
                f"  agent-{i}: source={label['source']:<8} version={label['version']} "
                f"age={label['age_seconds']:.2f}s"
            )

    s = await stats()
    print(f"\nupstream requests to SEC: {s['upstream_attempts']}")
    print(json.dumps(s, indent=2))


asyncio.run(main())