"""The scope monitor (design section 8, "Scope changes").

A scope that uses areas, devices, floors, labels or domains changes when
the user moves, adds or labels an entity. After each registry reload, the
monitor resolves the scope of every agent again. It compares the result
with the last stored result of the same definition version. If the scope
grew, it sends an HA notification and publishes a ``tools`` change event
for the management UI.

A new definition version is not growth: the user made that change. The
monitor then stores a new baseline and sends nothing. A scope with
``all=true`` and the ``admin`` level permit everything already, so they
cannot grow.

The monitor stores the last result in ``tool_scope_snapshots``. It can
therefore find growth that happened while the app was stopped.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial

from hagent_app.agents import AgentReader
from hagent_app.ha.client import HAClient, HAError
from hagent_app.http.events import ChangeEvents
from hagent_app.storage import Database
from hagent_app.tools.registry import RegistrySnapshot, RegistryUnavailableError
from hagent_app.tools.scope import agent_scope

_LOG = logging.getLogger(__name__)

TOPIC = "tools"
MAX_LISTED = 20


@dataclass(frozen=True, slots=True)
class ScopeGrowth:
    """The entities that one agent's scope gained."""

    agent_id: str
    agent_name: str
    version: int
    added_read: frozenset[str]
    added_control: frozenset[str]


def _load(
    conn: sqlite3.Connection, agent_id: str
) -> tuple[int, set[str], set[str]] | None:
    row = conn.execute(
        "SELECT agent_version, read_entities, control_entities"
        " FROM tool_scope_snapshots WHERE agent_id = ?",
        (agent_id,),
    ).fetchone()
    if row is None:
        return None
    return int(row[0]), set(json.loads(row[1])), set(json.loads(row[2]))


def _store(
    conn: sqlite3.Connection,
    agent_id: str,
    version: int,
    read: Iterable[str],
    control: Iterable[str],
) -> None:
    conn.execute(
        "INSERT INTO tool_scope_snapshots"
        " (agent_id, agent_version, read_entities, control_entities, resolved_at)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT (agent_id) DO UPDATE SET agent_version = excluded.agent_version,"
        " read_entities = excluded.read_entities,"
        " control_entities = excluded.control_entities,"
        " resolved_at = excluded.resolved_at",
        (
            agent_id,
            version,
            json.dumps(sorted(read)),
            json.dumps(sorted(control)),
            datetime.now(UTC).isoformat(),
        ),
    )


class ScopeMonitor:
    """Resolve every scope after a registry reload, and report growth."""

    def __init__(
        self,
        db: Database,
        reader: AgentReader,
        ha: HAClient,
        events: ChangeEvents | None = None,
        known_entities: Iterable[str] | None = None,
    ) -> None:
        self._db = db
        self._reader = reader
        self._ha = ha
        self._events = events
        self._known = known_entities

    def _known_ids(self, snapshot: RegistrySnapshot) -> set[str]:
        known = set(snapshot.entities)
        if self._known is not None:
            known |= set(self._known)
        return known

    async def on_reload(self, snapshot: RegistrySnapshot) -> None:
        """The registry reload listener."""
        await self.check(snapshot)

    async def check(self, snapshot: RegistrySnapshot) -> list[ScopeGrowth]:
        """Resolve every scope. Notify and return the scopes that grew."""
        known = self._known_ids(snapshot)
        growth = []
        for agent in await self._reader.list_agents():
            try:
                scope = agent_scope(agent.definition, snapshot)
            except RegistryUnavailableError:
                continue
            if scope.admin:
                continue
            read = scope.read.entity_set(known)
            control = scope.control.entity_set(known) | scope.outputs
            previous = await self._db.run(partial(_load, agent_id=agent.id))
            await self._db.run(
                partial(
                    _store,
                    agent_id=agent.id,
                    version=agent.version,
                    read=read,
                    control=control,
                )
            )
            if previous is None or previous[0] != agent.version:
                continue
            added_read = (
                frozenset() if scope.read.all else frozenset(read - previous[1])
            )
            added_control = (
                frozenset() if scope.control.all else frozenset(control - previous[2])
            )
            if added_read or added_control:
                growth.append(
                    ScopeGrowth(
                        agent.id, agent.name, agent.version, added_read, added_control
                    )
                )
        for item in growth:
            await self._report(item)
        return growth

    async def _report(self, growth: ScopeGrowth) -> None:
        added = sorted(growth.added_read | growth.added_control)
        listed = ", ".join(added[:MAX_LISTED])
        if len(added) > MAX_LISTED:
            listed += f", and {len(added) - MAX_LISTED} more"
        message = (
            f"The scope of agent '{growth.agent_name}' grew after a change to the "
            f"HA registries. It now includes: {listed}. Check the agent in hagent."
        )
        _LOG.warning(
            "scope of agent %s grew by %d entities", growth.agent_id, len(added)
        )
        if self._events is not None:
            self._events.publish(
                TOPIC,
                "scope_grown",
                growth.agent_id,
                {
                    "version": growth.version,
                    "added_read": sorted(growth.added_read),
                    "added_control": sorted(growth.added_control),
                },
            )
        try:
            await self._ha.call_service(
                "persistent_notification",
                "create",
                {
                    "title": "hagent: an agent scope grew",
                    "message": message,
                    "notification_id": f"hagent_scope_{growth.agent_id}",
                },
            )
        except (HAError, TimeoutError) as err:
            _LOG.warning("cannot send the scope notification: %s", err)
