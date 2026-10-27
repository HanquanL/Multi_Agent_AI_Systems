"""Allocation policies: which waiting request the rate limiter admits next.

One level of scheduling for now: requests are grouped by workload (an app or team the
operator configures), and the policy picks among waiting requests each time a slot frees up.
"""

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

DEFAULT_WORKLOAD = "default"


@dataclass(eq=False)
class Waiter:
    """One request waiting for a rate-limiter slot."""

    workload: str  # the bucket the policy assigned, not necessarily the caller's name
    agent: str
    admitted: asyncio.Future[None]


class AllocationPolicy(Protocol):
    def bucket(self, workload: str) -> str:
        """Map a caller's workload name to the bucket its usage is counted in."""
        ...

    def choose(self, waiting: Sequence[Waiter], usage: Mapping[str, int]) -> Waiter:
        """Pick the next waiter to admit. waiting is in arrival order and never empty;
        usage counts admissions per bucket in the current window."""
        ...


class QuotaFIFO:
    """Arrival order, with a per-workload cap on admissions per window.

    A workload at its quota is skipped while another workload is waiting under its own
    quota. If every waiter is at quota, the oldest is admitted anyway: idle capacity is
    lent out rather than wasted (work-conserving). Workloads not listed in quotas share
    one bucket, DEFAULT_WORKLOAD, capped by default_quota (None = no cap).
    With no quotas at all this is plain FIFO.
    """

    def __init__(
        self, quotas: Mapping[str, int] | None = None, *, default_quota: int | None = None
    ):
        self.quotas = dict(quotas or {})
        if any(q < 1 for q in self.quotas.values()):
            raise ValueError("quotas must be at least 1")
        if default_quota is not None and default_quota < 1:
            raise ValueError("default_quota must be at least 1")
        self.default_quota = default_quota

    def bucket(self, workload: str) -> str:
        return workload if workload in self.quotas else DEFAULT_WORKLOAD

    def choose(self, waiting: Sequence[Waiter], usage: Mapping[str, int]) -> Waiter:
        for waiter in waiting:
            quota = self.quotas.get(waiter.workload, self.default_quota)
            if quota is None or usage.get(waiter.workload, 0) < quota:
                return waiter
        return waiting[0]
