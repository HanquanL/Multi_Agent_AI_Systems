"""get_fact service: answers EDGAR fact questions through the gateway and reuses them while
their dependencies hold (docs/xbrl-selection.md).

Plugin glue: the rules and probes are in edgar_facts.py, the generic answer cache in
answers.py. Facts are computed by code from public filings, so answers are shared by all
workloads; the records behind them are fetched and charged like any other request.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from multi_agent_ai_systems.allocation import DEFAULT_WORKLOAD
from multi_agent_ai_systems.answers import AnswerCache, Outcome
from multi_agent_ai_systems.cache import RecordCache
from multi_agent_ai_systems.connectors.base import NotFoundError, SourceError
from multi_agent_ai_systems.connectors.edgar import (
    companyfacts_key,
    normalize_cik,
    submissions_key,
)
from multi_agent_ai_systems.connectors.edgar_facts import (
    METRICS,
    Fact,
    FactError,
    FactErrorKind,
    dependencies,
    evaluate,
    parse_period,
    resolve_fact,
)
from multi_agent_ai_systems.dependencies import Dependency
from multi_agent_ai_systems.gateway import Gateway
from multi_agent_ai_systems.inference import ContextError, Grounding


@dataclass(frozen=True)
class FactResult:
    answer: dict[str, Any]  # the fact and its provenance (R8), as JSON
    outcome: Outcome  # MATCH: reused; otherwise computed now, and why


class FactService:
    def __init__(self, gateway: Gateway, *, cache_bytes: int = 16 * 1024 * 1024):
        self.gateway = gateway
        self.answers = AnswerCache(gateway, evaluate, RecordCache(cache_bytes))

    async def get_fact(
        self,
        cik: int | str,
        metric: str,
        period: str,
        *,
        provenance: bool = False,
        max_age: float | None = None,
        workload: str = DEFAULT_WORKLOAD,
        agent: str = "anonymous",
    ) -> FactResult:
        """Raises ValueError for a bad CIK and FactError when the rules give no answer. An
        invalid question fails before anything is fetched. provenance=True also invalidates
        the answer when a newer filing reports the same value."""
        cik = validate_question(cik, metric, period)
        key = f"get_fact/{cik}/{metric}/{period}" + ("/provenance" if provenance else "")

        lookup = await self.answers.get(key, max_age=max_age, workload=workload, agent=agent)
        if lookup.outcome is Outcome.MATCH:
            return FactResult(lookup.answer, lookup.outcome)

        fact, deps = await self.compute(
            cik,
            metric,
            period,
            provenance=provenance,
            max_age=max_age,
            workload=workload,
            agent=agent,
        )
        answer = fact_payload(fact)
        self.answers.put(key, answer, deps)
        return FactResult(answer, lookup.outcome)

    async def compute(
        self,
        cik: str,
        metric: str,
        period: str,
        *,
        provenance: bool = False,
        max_age: float | None = None,
        workload: str = DEFAULT_WORKLOAD,
        agent: str = "anonymous",
    ) -> tuple[Fact, list[Dependency]]:
        """Resolve a validated question on records read through the gateway."""
        bodies = [
            (await self.gateway.get(k, max_age=max_age, workload=workload, agent=agent)).data
            for k in (companyfacts_key(cik), submissions_key(cik))
        ]
        fact = resolve_fact(*bodies, metric, period)
        return fact, dependencies(cik, period, fact, provenance=provenance)


def fact_payload(fact: Fact) -> dict[str, Any]:
    return {
        "value": fact.value,
        "unit": fact.unit,
        "metric": fact.metric,
        "concept": fact.concept,
        "period": fact.period,
        "start": fact.start.isoformat() if fact.start else None,
        "end": fact.end.isoformat(),
        "form": fact.form,
        "accn": fact.accn,
        "filed": fact.filed.isoformat(),
        "answer_kind": fact.answer_kind,
    }

def validate_question(cik: int | str, metric: str, period: str) -> str:
    """The normalized CIK. Raises ValueError or FactError without fetching anything."""
    normalized = normalize_cik(cik)
    if metric not in METRICS:
        raise FactError(FactErrorKind.UNSUPPORTED, f"unsupported metric: {metric!r}")
    parse_period(period)
    return normalized


class FactContext:
    """Grounds LLM prompts in reported facts. A specification is "CIK:metric:period".

    The prompt gives values and periods but not filings, so a response cannot name a source
    and value-only dependencies are enough.
    """

    def __init__(self, service: FactService):
        self.service = service
        self.checker = service.answers.checker

    def normalize(self, spec: str) -> str:
        parts = spec.split(":")
        if len(parts) != 3:
            raise ContextError(f"expected CIK:metric:period, got {spec!r}")
        cik, metric, period = parts
        try:
            return f"{validate_question(cik, metric, period)}:{metric}:{period}"
        except (ValueError, FactError) as exc:
            raise ContextError(f"{spec}: {exc}") from exc

    async def ground(
        self,
        specs: Sequence[str],
        *,
        max_age: float | None = None,
        workload: str = DEFAULT_WORKLOAD,
        agent: str = "anonymous",
    ) -> Grounding:
        lines = []
        found: list[Dependency] = []
        for spec in specs:
            cik, metric, period = spec.split(":")
            try:
                fact, deps = await self.service.compute(
                    cik, metric, period, max_age=max_age, workload=workload, agent=agent
                )
            except FactError as exc:
                raise ContextError(f"{spec}: {exc.kind.value}: {exc}") from exc
            lines.append(_fact_line(cik, fact))
            found.extend(deps)
        text = "Figures reported in SEC filings. Use these values exactly:\n" + "\n".join(lines)
        return Grounding(text, tuple(found))


def _fact_line(cik: str, fact: Fact) -> str:
    dates = f"{fact.start} to {fact.end}" if fact.start else f"at {fact.end}"
    return f"CIK {cik}: {fact.metric}, {fact.period} ({dates}): {fact.value} {fact.unit}"

def register_tools(server: MCPServer, service: FactService) -> None:
    @server.tool()
    async def get_fact(
        cik: str,
        metric: str,
        period: str,
        provenance: bool = False,
        max_age_seconds: float | None = None,
        workload: str = DEFAULT_WORKLOAD,
        agent_id: str = "anonymous",
    ) -> dict[str, Any]:
        """One reported financial value from a company's SEC filings, with its source.

        metric: Revenue, NetIncome, OperatingIncome, EPSDiluted, TotalAssets, or
        CashAndEquivalents. period: FY2024 (fiscal year), 2024Q2 (fiscal quarter),
        latest_annual, latest_quarter, or latest_reported_quarter. The answer gives the value,
        unit, period, dates, and the filing it came from, and "cache": "match" when it was
        reused because nothing it depends on changed. It is an error, never a guess, when the
        filings do not answer the question (for example a fiscal Q4, which companies report
        only inside the annual value). Set provenance to true if you rely on the answer naming
        the newest filing that reports the value.
        """
        # workload and agent_id are taken on trust until access keys exist.
        try:
            result = await service.get_fact(
                cik,
                metric,
                period,
                provenance=provenance,
                max_age=max_age_seconds,
                workload=workload,
                agent=agent_id,
            )
        except FactError as exc:
            raise ToolError(f"{exc.kind.value}: {exc}") from exc
        except NotFoundError as exc:
            raise ToolError(f"not found: no SEC data for CIK {cik}") from exc
        except ValueError as exc:
            raise ToolError(f"bad input: {exc}") from exc
        except SourceError as exc:
            raise ToolError(f"source error: {exc}") from exc
        return {**result.answer, "cache": result.outcome.value}