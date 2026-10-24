"""Request merging: concurrent requests for the same key share one upstream fetch."""

import asyncio
from collections.abc import Callable, Coroutine
from functools import partial
from typing import Any

from multi_agent_ai_systems.connectors.base import Record

Fetch = Callable[[str], Coroutine[Any, Any, Record]]


class RequestMerger:
    """Runs at most one fetch per key at a time; everyone asking meanwhile shares it.

    The fetch runs as its own task, so a caller that is cancelled or times out never
    cancels it for the others. When the fetch finishes, successfully or not, the key is
    cleared, so the next request starts a fresh fetch instead of reusing an old failure.
    The merger does not cache: a request after the fetch finishes fetches again.
    """

    def __init__(self, fetch: Fetch):
        self._fetch = fetch
        self._inflight: dict[str, asyncio.Task[Record]] = {}
        self.fetches_started = 0

    async def get(self, key: str) -> Record:
        task = self._inflight.get(key)
        if task is None or task.done():
            task = asyncio.create_task(self._fetch(key))
            task.add_done_callback(partial(self._finished, key))
            self._inflight[key] = task
            self.fetches_started += 1
        return await asyncio.shield(task)

    def _finished(self, key: str, task: asyncio.Task[Record]) -> None:
        if self._inflight.get(key) is task:
            del self._inflight[key]
        if not task.cancelled():
            task.exception()  # mark the error as seen, even if every caller already left

    async def close(self) -> None:
        """Cancel all in-flight fetches, e.g. on shutdown."""
        tasks = list(self._inflight.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
