"""The HA state cache (design section 13.2).

The cache keeps a current copy of every entity state. Tools and rule
templates read states from the cache, not from HA, so that all reads in one
invocation see the same data.

On each connect, the cache loads all states again:

1. The client subscribes to ``state_changed`` events first. The cache holds
   the events that arrive while it loads.
2. The cache calls ``get_states`` and builds a new copy from the reply.
3. It applies the held events in arrival order. It skips an event that is
   older than the state it already holds, compared by ``last_updated``.

When the connection closes, the cache keeps its data and sets ``stale``.
Section 18 needs this: rules continue to work while HA Core restarts.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any

from hagent_app.ha.client import Event, HAClient, Subscription

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class State:
    """The state of one entity, as HA reports it."""

    entity_id: str
    state: str
    attributes: Mapping[str, Any]
    last_changed: datetime
    last_updated: datetime
    context: Mapping[str, Any]

    @classmethod
    def from_ha(cls, data: Mapping[str, Any]) -> State:
        """Build a state from an HA state object."""
        last_updated = _parse_time(data.get("last_updated"))
        return cls(
            entity_id=str(data["entity_id"]),
            state=str(data.get("state", "")),
            attributes=MappingProxyType(dict(data.get("attributes") or {})),
            last_changed=_parse_time(data.get("last_changed"), last_updated),
            last_updated=last_updated,
            context=MappingProxyType(dict(data.get("context") or {})),
        )


def _parse_time(value: Any, default: datetime | None = None) -> datetime:
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    if default is not None:
        return default
    msg = f"HA state has no valid time: {value!r}"
    raise ValueError(msg)


class StateCache:
    """A current copy of all HA entity states."""

    def __init__(self, client: HAClient) -> None:
        self._client = client
        self._states: dict[str, State] = {}
        self._held: list[Event] | None = None
        self._ready = asyncio.Event()
        self._sub: Subscription | None = None
        self._synced = False

    @property
    def ready(self) -> bool:
        """True after the first full load."""
        return self._ready.is_set()

    @property
    def stale(self) -> bool:
        """True when the cache may not match HA.

        The cache is stale when the client has no connection, or when the
        last load after a connect did not finish.
        """
        return not self._client.connected or not self._synced

    async def wait_ready(self) -> None:
        """Wait until the first full load is complete.

        Wrap the call in ``asyncio.timeout`` to limit the wait.
        """
        await self._ready.wait()

    def get(self, entity_id: str) -> State | None:
        """Return the state of one entity, or ``None``."""
        return self._states.get(entity_id)

    def __contains__(self, entity_id: object) -> bool:
        return entity_id in self._states

    def __iter__(self) -> Iterator[State]:
        return iter(list(self._states.values()))

    def __len__(self) -> int:
        return len(self._states)

    async def start(self) -> None:
        """Subscribe to state changes and load the states on each connect."""
        self._client.add_connect_listener(self._reload)
        self._client.add_disconnect_listener(self._on_disconnect)
        self._sub = await self._client.subscribe_events(self._on_event, "state_changed")
        if self._client.connected:
            await self._reload()

    async def stop(self) -> None:
        """Remove the subscription."""
        if self._sub is not None:
            await self._sub.unsubscribe()
            self._sub = None

    async def _on_disconnect(self) -> None:
        self._synced = False

    async def _reload(self) -> None:
        self._synced = False
        self._held = []
        try:
            raw = await self._client.get_states()
            states = {}
            for item in raw:
                state = State.from_ha(item)
                states[state.entity_id] = state
            held, self._held = self._held, None
            self._states = states
            for event in held:
                self._apply(event)
        finally:
            self._held = None
        self._synced = True
        self._ready.set()
        _LOG.info("state cache loaded %d entities", len(self._states))

    def _on_event(self, event: Event) -> None:
        if self._held is not None:
            self._held.append(event)
            return
        self._apply(event)

    def _apply(self, event: Event) -> None:
        data = event.get("data") or {}
        entity_id = data.get("entity_id")
        if not isinstance(entity_id, str):
            return
        current = self._states.get(entity_id)
        new = data.get("new_state")
        try:
            if new is None:
                fired = _parse_time(event.get("time_fired"))
                if current is not None and current.last_updated <= fired:
                    del self._states[entity_id]
                return
            state = State.from_ha(new)
        except KeyError, ValueError:
            _LOG.warning("bad state_changed event for %s", entity_id)
            return
        if current is None or state.last_updated >= current.last_updated:
            self._states[entity_id] = state
