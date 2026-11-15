"""The store interfaces that the knowledge, memory and self tools use.

Other tasks build the stores: task-11 the knowledge stores, task-10 the
history, task-13 to task-15 the rules, schedules and subscriptions. The
tools depend only on these protocols, so that each store plugs in later.

The executor checks the scope before it calls a store. It passes the agent
ID from the invocation, never from the model arguments, and it passes only
attached store IDs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal, Protocol

type StateKind = Literal["rule", "schedule", "subscription"]
type WriteMode = Literal["create", "append", "replace"]


class StoreError(Exception):
    """A store refused a call, for example on a version conflict.

    The executor returns ``code`` to the model as the ``error_code``.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class EntitySearch(Protocol):
    """Ranked search over the HA entity catalogue (design section 13.3)."""

    async def rank_entities(
        self, query: str, entity_ids: Sequence[str], limit: int
    ) -> list[str]:
        """The IDs from ``entity_ids`` that match ``query``, best first.

        The caller passes only entities in the agent's scope. The result
        holds no other ID.
        """
        ...


class KnowledgeStore(Protocol):
    """Knowledge store content (design section 13.4)."""

    async def describe(self, store_ids: Sequence[str]) -> list[dict[str, Any]]:
        """Name, description and size of each store."""
        ...

    async def list_items(self, store_id: str) -> list[dict[str, Any]]:
        """The files and documents in one store."""
        ...

    async def read(
        self, store_id: str, path: str, section: str | None
    ) -> dict[str, Any]:
        """One file or document, or one section of it."""
        ...

    async def search(
        self, store_ids: Sequence[str], query: str, limit: int, *, agent_id: str
    ) -> list[dict[str, Any]]:
        """Hybrid search across the named stores only.

        ``agent_id`` is the invocation agent. The query embedding counts
        towards its budget.
        """
        ...

    async def write(  # noqa: PLR0913
        self,
        store_id: str,
        path: str,
        mode: WriteMode,
        content: str,
        *,
        old_text: str | None,
        base_version: int | None,
        actor_agent_id: str,
    ) -> dict[str, Any]:
        """Create, append to or change a file. Fail on a version conflict."""
        ...

    async def delete_file(
        self, store_id: str, path: str, *, base_version: int | None, actor_agent_id: str
    ) -> None:
        """Delete a file. Documents cannot be deleted this way."""
        ...


class HistoryStore(Protocol):
    """Invocation history and rule statistics (design sections 10.4, 13.3)."""

    async def search(
        self, agent_id: str, query: str, filters: Mapping[str, Any], limit: int
    ) -> list[dict[str, Any]]:
        """Search the history of one agent."""
        ...

    async def rule_owner(self, rule_id: str) -> str | None:
        """The ID of the agent that owns the rule, or ``None``."""
        ...

    async def rule_stats(self, agent_id: str, rule_id: str) -> dict[str, Any]:
        """The statistics of one rule."""
        ...


class AgentStateStore(Protocol):
    """Rules, schedules and subscriptions: the agent state (design section 7)."""

    async def owner(self, kind: StateKind, item_id: str) -> str | None:
        """The ID of the agent that owns the item, or ``None``."""
        ...

    async def list_items(self, agent_id: str, kind: StateKind) -> list[dict[str, Any]]:
        """The items of one kind that the agent owns."""
        ...

    async def create(
        self, agent_id: str, kind: StateKind, spec: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Create an item for the agent."""
        ...

    async def update(
        self, agent_id: str, kind: StateKind, item_id: str, spec: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Change an item of the agent."""
        ...

    async def set_enabled(
        self, agent_id: str, kind: StateKind, item_id: str, enabled: bool
    ) -> None:
        """Turn an item of the agent on or off."""
        ...

    async def delete(self, agent_id: str, kind: StateKind, item_id: str) -> None:
        """Delete an item of the agent."""
        ...
