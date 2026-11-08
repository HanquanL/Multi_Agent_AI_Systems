"""LLM generation requests, the provider interface, and the exact-match response key.

Domain-agnostic. Providers don't cache, merge or rate-limit; the gateway does that, using the
limits each provider declares. Text generation only, non-streaming, one model per provider.
"""

import hashlib
import json
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass

from multi_agent_ai_systems.connectors.base import RateLimit

ROLES = ("system", "user", "assistant")
_KEY_FORMAT = "llm.v1"  # change it if the key's contents change, so old entries can't match


@dataclass(frozen=True)
class Message:
    role: str
    content: str


@dataclass(frozen=True)
class GenerationRequest:
    messages: tuple[Message, ...]
    max_tokens: int  # required: the most this call can cost
    temperature: float = 0.0
    top_p: float | None = None
    seed: int | None = None
    stop: tuple[str, ...] = ()
    allow_reuse: bool = False  # the caller accepts a shared answer despite temperature > 0

    def __post_init__(self) -> None:
        if not self.messages:
            raise ValueError("a request needs at least one message")
        if any(m.role not in ROLES for m in self.messages):
            raise ValueError(f"message roles must be one of {ROLES}")
        if self.max_tokens < 1:
            raise ValueError("max_tokens must be at least 1")
        if self.temperature < 0:
            raise ValueError("temperature must not be negative")

    @property
    def reusable(self) -> bool:
        """May this request be cached or merged? Otherwise callers would silently share one
        random sample."""
        return self.temperature == 0 or self.allow_reuse


@dataclass(frozen=True)
class Completion:
    text: str
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str  # "stop" when the model finished; "length" when cut off


@dataclass(frozen=True)
class ProviderLimits:
    requests: RateLimit
    tokens: RateLimit | None = None  # tokens per period; None if the provider sets no such limit

@dataclass(frozen=True)
class ModelPrice:
    """Published prices, USD per million tokens. Gives an estimate, never a bill."""

    input_per_million: float
    output_per_million: float

    def usd(self, prompt_tokens: int, completion_tokens: int) -> float:
        return (
            prompt_tokens * self.input_per_million
            + completion_tokens * self.output_per_million
        ) / 1_000_000

class InferenceProvider(ABC):
    """One model behind one API."""

    name: str
    model: str
    limits: ProviderLimits

    @abstractmethod
    async def model_version(self) -> str:
        """Identifies the exact model, so a model update cannot reuse old answers."""

    @abstractmethod
    async def generate(self, request: GenerationRequest) -> Completion:
        """Make at most one upstream call. Never retry; the gateway decides that."""


def request_key(
    request: GenerationRequest,
    *,
    scope: str,
    provider: str,
    model: str,
    model_version: str,
    context: Sequence[str] = (),
) -> str:
    """The exact-match key: equal only if everything that can change the answer is equal.
    scope is who may share the answer. context lists what the gateway adds to the prompt, such
    as fact specifications."""
    body = {
        "format": _KEY_FORMAT,
        "scope": scope,
        "provider": provider,
        "model": model,
        "model_version": model_version,
        "context": list(context),
        "messages": [[m.role, m.content] for m in request.messages],
        "max_tokens": request.max_tokens,
        "temperature": float(request.temperature),  # 0 and 0.0 are the same setting
        "top_p": None if request.top_p is None else float(request.top_p),
        "seed": request.seed,
        "stop": list(request.stop),
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "llm/" + hashlib.sha256(canonical.encode()).hexdigest()

def estimate_tokens(request: GenerationRequest) -> int:
    """Tokens to reserve before sending: about 4 characters per input token, plus the whole
    output allowance, since the real count is known only afterwards."""
    return sum(len(m.content) // 4 + 4 for m in request.messages) + request.max_tokens