"""The single path from the gateway to a source: every attempt passes the rate limiter."""

from multi_agent_ai_systems.clock import Clock, RealClock
from multi_agent_ai_systems.connectors.base import (
    Connector,
    NotFoundError,
    Record,
    SourceError,
    ThrottledError,
)
from multi_agent_ai_systems.ratelimit import RateLimiter


class Upstream:
    """Fetches from one connector, with rate limiting and optional retries.

    Every attempt, retries included, waits for the shared limiter first. Missing records
    (NotFoundError) and bad keys (ValueError) are never retried. A bad key costs one
    limiter slot but sends no request.
    """

    def __init__(
        self,
        connector: Connector,
        limiter: RateLimiter,
        *,
        max_attempts: int = 1,
        backoff_seconds: float = 1.0,
        clock: Clock | None = None,
    ):
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._connector = connector
        self._limiter = limiter
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        self._clock = clock or RealClock()
        self.attempts = 0

    async def fetch(self, key: str) -> Record:
        attempt = 0
        while True:
            attempt += 1
            await self._limiter.acquire()
            self.attempts += 1
            try:
                return await self._connector.fetch(key)
            except NotFoundError:
                raise
            except SourceError as exc:
                if attempt >= self.max_attempts:
                    raise
                await self._clock.sleep(self._retry_delay(exc))

    def _retry_delay(self, exc: SourceError) -> float:
        if isinstance(exc, ThrottledError) and exc.retry_after is not None:
            return exc.retry_after
        return self.backoff_seconds
