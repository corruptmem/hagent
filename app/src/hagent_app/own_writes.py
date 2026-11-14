"""Link HA state changes to the service calls that caused them.

HA creates a context for each WebSocket ``call_service`` command. A context is
a record that HA attaches to a service call and to each state change that the
call causes. The client cannot supply the context. HA returns it in the
command result. The ``state_changed`` event that the call causes carries the
same context ID, but HA sends the event before the result. See task-2 and
docs/design.md section 12.

``OwnWriteTracker`` therefore holds an event with an unknown context ID while
a call is in flight. When the call result arrives, the tracker classifies the
held events. An event is ``agent`` when its context ID belongs to a call that
an agent made. Otherwise it is ``external``.

The module is pure. It does no I/O and takes its clock as a parameter.
"""

from __future__ import annotations

import itertools
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

type Origin = Literal["agent", "external"]


@dataclass(frozen=True, slots=True)
class Classified[T]:
    """An event with its origin and, for an agent write, the agent ID."""

    payload: T
    context_id: str
    origin: Origin
    agent_id: str | None


@dataclass(slots=True)
class _Held[T]:
    payload: T
    context_id: str
    since: float


class OwnWriteTracker[T]:
    """Classify state changes as agent writes or external changes.

    Use it like this:

    1. Call ``call_started`` before you send ``call_service``.
    2. Call ``call_finished`` with the context ID from the result. Pass
       ``None`` if the call failed.
    3. Call ``event`` for each ``state_changed`` event.
    4. Call ``tick`` from time to time, to release old held events and to
       forget old context IDs.

    Each method returns the events that are now classified, in arrival order.

    ``hold`` is the longest time in seconds that the tracker holds an event
    while it waits for a call result. ``ttl`` is the time in seconds for which
    the tracker keeps the context ID of a finished call. One call can cause
    several state changes, or none, so the tracker keeps the ID until the TTL
    ends.
    """

    def __init__(
        self,
        *,
        hold: float = 5.0,
        ttl: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._hold = hold
        self._ttl = ttl
        self._clock = clock
        self._tokens = itertools.count(1)
        self._in_flight: dict[int, str] = {}
        self._known: dict[str, tuple[str, float]] = {}
        self._held: deque[_Held[T]] = deque()

    @property
    def in_flight(self) -> int:
        """The number of calls that have no result yet."""
        return len(self._in_flight)

    @property
    def held(self) -> int:
        """The number of events that wait for classification."""
        return len(self._held)

    def call_started(self, agent_id: str) -> int:
        """Record a call that an agent starts. Return a token for the call."""
        token = next(self._tokens)
        self._in_flight[token] = agent_id
        return token

    def call_finished(self, token: int, context_id: str | None) -> list[Classified[T]]:
        """Record the result of a call and release the events it resolves."""
        agent_id = self._in_flight.pop(token)
        if context_id is not None:
            self._known[context_id] = (agent_id, self._clock())
        return self._release()

    def event(self, context_id: str, payload: T) -> list[Classified[T]]:
        """Add a state change event. Return the events that are now classified."""
        self._held.append(_Held(payload, context_id, self._clock()))
        return self._release()

    def tick(self) -> list[Classified[T]]:
        """Forget expired context IDs and release events held too long."""
        now = self._clock()
        expired = [c for c, (_, t) in self._known.items() if now - t > self._ttl]
        for context_id in expired:
            del self._known[context_id]
        return self._release()

    def _release(self) -> list[Classified[T]]:
        now = self._clock()
        out: list[Classified[T]] = []
        while self._held:
            head = self._held[0]
            known = self._known.get(head.context_id)
            if known is not None:
                origin: Origin = "agent"
                agent_id: str | None = known[0]
            elif not self._in_flight or now - head.since >= self._hold:
                origin, agent_id = "external", None
            else:
                break
            self._held.popleft()
            out.append(Classified(head.payload, head.context_id, origin, agent_id))
        return out
