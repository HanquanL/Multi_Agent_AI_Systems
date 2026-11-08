"""MCP server: exposes a Gateway to agents as tools.

Run one server over streamable HTTP so all agents share one gateway (one cache, one rate
limiter). Over stdio, every agent starts its own server process and nothing is shared.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from multi_agent_ai_systems.allocation import DEFAULT_WORKLOAD
from multi_agent_ai_systems.answers import AnswerCache
from multi_agent_ai_systems.connectors.base import NotFoundError, SourceError
from multi_agent_ai_systems.gateway import Gateway, Response, WorkloadCounts
from multi_agent_ai_systems.inference import ContextError, InferenceGateway
from multi_agent_ai_systems.llm import GenerationRequest, Message


def build_server(
    gateway: Gateway,
    *,
    name: str = "shared-data-gateway",
    instructions: str | None = None,
    tools: Sequence[Callable[[MCPServer], None]] = (),
    answers: Mapping[str, AnswerCache] | None = None,
    inference: InferenceGateway | None = None,
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
        fetched, its age, and whether it came from the memory cache, the disk store, or the
        source.
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
        """Cache, upstream, rate-limit and answer-cache counters for this gateway."""
        return stats_payload(gateway, answers, inference)

    if inference is not None:
        _add_generate(server, inference)
    for add_tools in tools:  # plugins add their own tools, e.g. a domain's question types
        add_tools(server)
    return server

def _add_generate(server: MCPServer, inference: InferenceGateway) -> None:
    @server.tool()
    async def generate(
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float = 0.0,
        top_p: float | None = None,
        seed: int | None = None,
        stop: list[str] | None = None,
        allow_reuse: bool = False,
        no_store: bool = False,
        context: list[str] | None = None,
        max_age_seconds: float | None = None,
        data_max_age_seconds: float | None = None,
        workload: str = DEFAULT_WORKLOAD,
        agent_id: str = "anonymous",
    ) -> dict[str, Any]:
        """Generate text with the gateway's LLM, reusing a stored answer when that is safe.

        messages is a list of {"role": "system"|"user"|"assistant", "content": text}. An
        identical request from your workload is answered without another model call. Requests
        with temperature above 0 are not reused unless allow_reuse is true; no_store keeps a
        request out of the cache. context lists data the gateway adds to the prompt; the
        answer is reused only while that data is unchanged. max_age_seconds refuses stored
        answers older than that; data_max_age_seconds does the same for the data they are
        checked against. The result's source says where it came from ("cache", "store",
        "merged" or "upstream"). answer_kind "generated" means it is unverified model output.
        workload is the app or team you belong to (set by your operator).
        """
        # workload and agent_id are taken on trust until access keys exist.
        try:
            request = GenerationRequest(
                messages=tuple(Message(m["role"], m["content"]) for m in messages),
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p,
                seed=seed,
                stop=tuple(stop or ()),
                allow_reuse=allow_reuse,
                no_store=no_store,
            )
            result = await inference.generate(
                request,
                context=context or (),
                max_age=max_age_seconds,
                data_max_age=data_max_age_seconds,
                workload=workload,
                agent=agent_id,
            )
        except ContextError as exc:
            raise ToolError(f"context error: {exc}") from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise ToolError(f"bad request: {exc}") from exc
        except SourceError as exc:
            raise ToolError(f"source error: {exc}") from exc
        return asdict(result)
    
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


def stats_payload(
        gateway: Gateway,answers: Mapping[str, AnswerCache] | None = None,
        inference: InferenceGateway | None = None,
) -> dict[str, Any]:
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
        "store_hits": stats.store_hits,
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
        "store": store_payload(gateway),
        "rate_limit": {
            "max_requests": limiter.max_requests,
            "period_seconds": limiter.period,
        },
        "workloads": workloads,
        "answers": {name: answers_payload(cache) for name, cache in (answers or {}).items()},
        "inference": inference_payload(inference) if inference is not None else None,
    }


def store_payload(gateway: Gateway | InferenceGateway) -> dict[str, Any] | None:
    store = gateway.store
    if store is None:
        return None
    return {
        "entries": len(store),
        "used_bytes": store.used_bytes,
        "occupancy": round(store.occupancy, 4),
        "evictions": store.stats.evictions,
        "evicted_bytes": store.stats.evicted_bytes,
        "too_large": store.stats.too_large,
        "unstorable": store.stats.unstorable,
    }

def answers_payload(answers: AnswerCache) -> dict[str, Any]:
    return {
        "entries": len(answers),
        "outcomes": {outcome.value: n for outcome, n in answers.stats.outcomes.items()},
        "changed_probes": dict(answers.stats.changed_probes),
    }

def inference_payload(inference: InferenceGateway) -> dict[str, Any]:
    price = inference.price
    workloads = {}
    for bucket, counts in sorted(inference.stats.by_workload.items()):
        spent = (counts.prompt_tokens_spent, counts.completion_tokens_spent)
        avoided = (counts.prompt_tokens_avoided, counts.completion_tokens_avoided)
        workloads[bucket] = {
            **asdict(counts),
            "estimated_usd_spent": price.usd(*spent) if price is not None else None,
            "estimated_usd_avoided": price.usd(*avoided) if price is not None else None,
        }
    cache = inference.cache
    return {
        "provider": inference.provider.name,
        "model": inference.provider.model,
        "attempts": inference.attempts,
        "cache": {
            "entries": len(cache),
            "used_bytes": cache.used_bytes,
            "evictions": cache.stats.evictions,
        },
        "store": store_payload(inference),
        "changed_probes": dict(inference.stats.changed_probes),
        "workloads": workloads,
    }
