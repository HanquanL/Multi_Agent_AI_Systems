"""MCP server: exposes a Gateway to agents as tools.

Run one server over streamable HTTP so all agents share one gateway (one cache, one rate
limiter). Over stdio, every agent starts its own server process and nothing is shared.
"""

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from multi_agent_ai_systems.allocation import DEFAULT_WORKLOAD
from multi_agent_ai_systems.connectors.base import NotFoundError, SourceError
from multi_agent_ai_systems.gateway import Gateway, Response, WorkloadCounts


def build_server(
    gateway: Gateway,
    *,
    name: str = "shared-data-gateway",
    instructions: str | None = None,
) -> MCPServer:
    server = MCPServer(name, instructions=instructions)

    @server.tool()
    async def get_record(
        key: str,
        max_age_seconds: float | None = None,
        workload: str = DEFAULT_WORKLOAD,
        agent_id: str = "anonymous",
    ) -> dict[str, Any]:
        """Fetch a record through the shared gateway.

        Served from the shared cache when it is still valid, otherwise fetched from the
        source within the shared rate limit. Set max_age_seconds to refuse cached data older
        than that. workload is the app or team you belong to (set by your operator); it
        decides whose share of the rate limit a fetch uses. agent_id identifies you in logs.
        The result has the data plus a freshness label: the source version, when it was
        fetched, its age, and whether it came from the cache or the source.
        """
        # workload and agent_id are taken on trust until access keys exist.
        try:
            response = await gateway.get(
                key, max_age=max_age_seconds, workload=workload, agent=agent_id
            )
        except NotFoundError as exc:
            raise ToolError(f"not found: {key}") from exc
        except ValueError as exc:
            raise ToolError(f"bad key: {exc}") from exc
        except SourceError as exc:
            raise ToolError(f"source error: {exc}") from exc
        return to_payload(response)

    @server.tool()
    async def gateway_stats() -> dict[str, Any]:
        """Cache, upstream and rate-limit counters for this gateway."""
        return stats_payload(gateway)

    return server


def to_payload(response: Response) -> dict[str, Any]:
    label = response.label
    data = response.data
    if isinstance(data, bytes):
        data = data.decode("utf-8", errors="replace")
    return {
        "data": data,
        "freshness": {
            "key": label.key,
            "version": label.version,
            "fetched_at": label.fetched_at.isoformat(),
            "age_seconds": label.age_seconds,
            "source": label.source,
        },
    }


def stats_payload(gateway: Gateway) -> dict[str, Any]:
    stats, cache, limiter = gateway.stats, gateway.cache, gateway.limiter
    workloads = {}
    for bucket in sorted(stats.by_workload.keys() | limiter.stats.keys()):
        counts = stats.by_workload.get(bucket, WorkloadCounts())
        waits = limiter.stats.get(bucket)
        workloads[bucket] = {
            "hits": counts.hits,
            "misses": counts.misses,
            "fetches_started": counts.fetches_started,
            "limiter_admitted": waits.admitted if waits else 0,
            "max_wait_seconds": round(waits.max_wait, 4) if waits else 0.0,
        }
    return {
        "hits": stats.hits,
        "misses": stats.misses,
        "upstream_fetches": {reason.value: n for reason, n in stats.fetches.items()},
        "upstream_attempts": gateway.upstream.attempts,
        "cache": {
            "entries": len(cache),
            "used_bytes": cache.used_bytes,
            "occupancy": round(cache.occupancy, 4),
            "evictions": cache.stats.evictions,
            "evicted_bytes": cache.stats.evicted_bytes,
            "too_large": cache.stats.too_large,
        },
        "rate_limit": {
            "max_requests": limiter.max_requests,
            "period_seconds": limiter.period,
        },
        "workloads": workloads,
    }
