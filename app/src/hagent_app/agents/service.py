"""Agent definitions: the read interface and the write service.

``AgentReader`` only reads. Give it to every part of the runtime that needs
agent definitions, for example the tool executor (task-8) and the router.
It has no write method, and its results are frozen. No agent tool can
therefore change a definition (design section 7).

``AgentService`` reads and writes. Only the management API uses it. Each
write publishes a change event on the ``agents`` topic.
"""

from __future__ import annotations

from hagent_app.agents import store
from hagent_app.agents.definition import AgentDefinition
from hagent_app.agents.models import (
    Agent,
    AgentVersion,
    FieldChange,
    StoreChoice,
)
from hagent_app.http.events import ChangeEvents
from hagent_app.storage import Database

TOPIC = "agents"


class AgentReader:
    """Read-only access to agents and their definition versions."""

    __slots__ = ("_db",)

    def __init__(self, db: Database) -> None:
        self._db = db

    async def list_agents(self) -> list[Agent]:
        """Every live agent, sorted by name."""
        return await self._db.run(store.list_agents)

    async def get(self, agent_id: str) -> Agent | None:
        """The live agent and its current version, or ``None``."""
        return await self._db.run(lambda c: store.find_agent(c, agent_id))

    async def get_version(self, agent_id: str, version: int) -> AgentVersion | None:
        """One definition version, also of a deleted agent, or ``None``.

        History records name a version. Use this to read it.
        """
        return await self._db.run(lambda c: store.find_version(c, agent_id, version))


class AgentService:
    """Create, change and delete agents. For the management API only."""

    def __init__(self, db: Database, events: ChangeEvents) -> None:
        self._db = db
        self._events = events
        self.reader = AgentReader(db)

    def _publish(self, action: str, agent: Agent) -> None:
        self._events.publish(
            TOPIC,
            action,
            agent.id,
            {"version": agent.version, "enabled": agent.enabled},
        )

    async def list_agents(self) -> list[Agent]:
        """Every live agent."""
        return await self._db.run(store.list_agents)

    async def get(self, agent_id: str) -> Agent:
        """One live agent. Raise ``AgentNotFoundError``."""
        return await self._db.run(lambda c: store.get_agent(c, agent_id))

    async def create(self, definition: AgentDefinition) -> Agent:
        """Create an agent with its private store."""
        agent = await self._db.run(lambda c: store.create_agent(c, definition))
        self._publish("created", agent)
        return agent

    async def update(
        self,
        agent_id: str,
        definition: AgentDefinition,
        base_version: int | None = None,
    ) -> Agent:
        """Save a changed definition as a new version."""
        agent, changed = await self._db.run(
            lambda c: store.update_agent(
                c, agent_id, definition, base_version=base_version
            )
        )
        if changed:
            self._publish("updated", agent)
        return agent

    async def copy(self, agent_id: str, name: str | None = None) -> Agent:
        """Copy an agent. The copy gets its own private store."""
        agent = await self._db.run(lambda c: store.copy_agent(c, agent_id, name))
        self._publish("created", agent)
        return agent

    async def set_enabled(self, agent_id: str, enabled: bool) -> Agent:
        """Turn an agent on or off."""
        agent = await self._db.run(lambda c: store.set_enabled(c, agent_id, enabled))
        self._publish("updated", agent)
        return agent

    async def delete(self, agent_id: str, private_store: StoreChoice | None) -> None:
        """Delete an agent. ``private_store`` must be ``keep`` or ``delete``."""
        await self._db.run(lambda c: store.delete_agent(c, agent_id, private_store))
        self._events.publish(
            TOPIC, "deleted", agent_id, {"private_store": private_store}
        )

    async def versions(self, agent_id: str) -> list[AgentVersion]:
        """Every version of an agent, newest first."""
        return await self._db.run(lambda c: store.list_versions(c, agent_id))

    async def version(self, agent_id: str, version: int) -> AgentVersion:
        """One version of an agent."""
        return await self._db.run(lambda c: store.get_version(c, agent_id, version))

    async def diff(self, agent_id: str, old: int, new: int) -> list[FieldChange]:
        """The fields that differ between two versions."""
        a = await self.version(agent_id, old)
        b = await self.version(agent_id, new)
        return store.diff_definitions(a.definition, b.definition)

    async def restore(
        self, agent_id: str, version: int, base_version: int | None = None
    ) -> Agent:
        """Save an earlier version as the new current version."""
        agent, changed = await self._db.run(
            lambda c: store.restore_version(
                c, agent_id, version, base_version=base_version
            )
        )
        if changed:
            self._publish("updated", agent)
        return agent
