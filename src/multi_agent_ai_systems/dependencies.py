"""Dependencies of a cached answer: what it relied on, so reuse can be checked later.

Domain-agnostic. A plugin creates dependencies when it answers a question and evaluates their
probes later; the core only stores them and compares values.
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Dependency:
    keys: tuple[str, ...]  # records the probe reads, in the order the plugin expects
    probe: str  # versioned probe spec; only the plugin that made it knows what it means
    expected: Any  # JSON-compatible value the probe returned when the answer was made

def dependency_to_json(dep: Dependency) -> dict[str, Any]:
    return {"keys": list(dep.keys), "probe": dep.probe, "expected": dep.expected}


def dependency_from_json(item: dict[str, Any]) -> Dependency:
    return Dependency(tuple(item["keys"]), item["probe"], item["expected"])