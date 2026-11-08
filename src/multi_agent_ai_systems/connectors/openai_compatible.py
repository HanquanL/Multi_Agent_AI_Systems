"""Connector for any LLM API that follows OpenAI's chat completions format.

Works with local servers (Ollama's /v1) and hosted providers: only the URL, model and key differ.
"""

from typing import Any, Self

import httpx

from multi_agent_ai_systems.clock import Clock, RealClock
from multi_agent_ai_systems.connectors.base import SourceError, ThrottledError
from multi_agent_ai_systems.llm import (
    Completion,
    GenerationRequest,
    InferenceProvider,
    ProviderLimits,
)


class OpenAICompatibleProvider(InferenceProvider):
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        limits: ProviderLimits,
        name: str = "openai-compatible",
        api_key: str | None = None,
        version: str | None = None,  # pin it when the API cannot identify the exact model
        version_ttl: float = 300.0,
        timeout: float = 300.0,  # local models can take minutes
        clock: Clock | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.name = name
        self.model = model
        self.limits = limits
        self._base_url = base_url.rstrip("/")
        self._pinned_version = version
        self._version_ttl = version_ttl
        self._version: str | None = None
        self._version_checked = 0.0
        self._clock = clock or RealClock()
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.AsyncClient(headers=headers, timeout=timeout, transport=transport)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def model_version(self) -> str:
        if self._pinned_version is not None:
            return self._pinned_version
        now = self._clock.now()
        if self._version is not None and now < self._version_checked + self._version_ttl:
            return self._version
        try:
            self._version = await self._lookup_version()
        except SourceError:
            if self._version is None:
                raise  # otherwise keep the last known version through a brief outage
        self._version_checked = now
        return self._version

    async def _lookup_version(self) -> str:
        body = await self._request("GET", "/models")
        try:
            entry = next((m for m in body["data"] if m["id"] == self.model), None)
        except (KeyError, TypeError) as exc:
            raise SourceError(f"unexpected model list from {self.name}") from exc
        if entry is None:
            raise SourceError(f"{self.name} does not serve model {self.model}")
        return f"{self.model}@{entry.get('created', 'unknown')}"

    async def generate(self, request: GenerationRequest) -> Completion:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "stream": False,
        }
        if request.top_p is not None:
            payload["top_p"] = request.top_p
        if request.seed is not None:
            payload["seed"] = request.seed
        if request.stop:
            payload["stop"] = list(request.stop)

        body = await self._request("POST", "/chat/completions", payload)
        try:
            choice = body["choices"][0]
            usage = body["usage"]
            text = choice["message"]["content"]
            if not isinstance(text, str):
                raise TypeError("content is not text")
            return Completion(
                text=text,
                prompt_tokens=int(usage["prompt_tokens"]),
                completion_tokens=int(usage["completion_tokens"]),
                finish_reason=choice["finish_reason"],
            )
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise SourceError(f"unexpected response from {self.name}") from exc

    async def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            response = await self._client.request(method, self._base_url + path, json=payload)
        except httpx.TimeoutException as exc:
            raise SourceError(f"timed out calling {self.name}") from exc
        except httpx.TransportError as exc:
            raise SourceError(f"network error calling {self.name}: {exc}") from exc

        if response.status_code == 429:
            raise ThrottledError(
                f"throttled by {self.name}", retry_after=_retry_after(response)
            )
        if response.status_code != 200:
            raise SourceError(f"HTTP {response.status_code} from {self.name}")
        try:
            return response.json()
        except ValueError as exc:
            raise SourceError(f"bad JSON from {self.name}") from exc


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None