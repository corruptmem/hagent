"""Change events for the management UI and for other components.

A change event says that a record changed. The management UI receives the
events on the ``/api/events`` WebSocket and reloads the changed data. Other
components, for example the integration link, add an in-process listener
with ``ChangeEvents.subscribe``.

Each event is a JSON object::

    {"topic": "agents", "action": "updated", "id": "<agent ID>",
     "data": {"version": 3}}
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ChangeEvent:
    """One change: a topic, an action, the record ID and extra data."""

    topic: str
    action: str
    id: str
    data: Mapping[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        """The JSON object that the WebSocket sends."""
        return {
            "topic": self.topic,
            "action": self.action,
            "id": self.id,
            "data": dict(self.data),
        }


type Listener = Callable[[ChangeEvent], None]


class ChangeEvents:
    """A synchronous publish and subscribe hub for change events.

    A listener is a plain function. It must be fast and must not block. An
    exception in a listener does not stop the other listeners.
    """

    def __init__(self) -> None:
        self._listeners: list[Listener] = []

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        """Add a listener. Return a function that removes it."""
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    def publish(
        self,
        topic: str,
        action: str,
        record_id: str,
        data: Mapping[str, Any] | None = None,
    ) -> None:
        """Send an event to every listener."""
        event = ChangeEvent(topic, action, record_id, data or {})
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:
                _LOG.exception("change event listener failed")
