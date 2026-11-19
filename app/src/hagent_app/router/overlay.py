"""Sample states for a test invocation."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from typing import Any

from hagent_app.ha import State
from hagent_app.tools import StateView


def sample_state(entity_id: str, value: Mapping[str, Any]) -> State:
    """A ``State`` from a sample of the form ``{"state": ..., "attributes": {}}``."""
    stamp = datetime.now(UTC).isoformat()
    return State.from_ha(
        {
            "entity_id": entity_id,
            "state": value.get("state", ""),
            "attributes": value.get("attributes") or {},
            "last_changed": stamp,
            "last_updated": stamp,
        }
    )


class StateOverlay:
    """A state view: the sample states first, then the base view."""

    def __init__(self, base: StateView, samples: Mapping[str, State]) -> None:
        self._base = base
        self._samples = dict(samples)

    def get(self, entity_id: str) -> State | None:
        """The sample state, or the state of the base view."""
        sample = self._samples.get(entity_id)
        return sample if sample is not None else self._base.get(entity_id)

    @property
    def ready(self) -> bool:
        """The base view decides: sample states never make old data current."""
        return self._base.ready

    @property
    def stale(self) -> bool:
        """The base view decides."""
        return self._base.stale

    def __iter__(self) -> Iterator[State]:
        yield from self._samples.values()
        for state in self._base:
            if state.entity_id not in self._samples:
                yield state
