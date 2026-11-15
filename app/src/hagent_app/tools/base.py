"""The parts that every tool shares.

A tool has two steps:

1. ``authorize`` checks the arguments against the scope and returns a
   ``Prepared`` request. It must not change anything.
2. ``run`` acts on the prepared request only, never on the raw arguments.

The executor runs ``authorize``, the approval check and the audit write
before ``run``. A tool reaches HA or a store only in ``run``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, ConfigDict

from hagent_app.agents.models import Agent

if TYPE_CHECKING:
    from hagent_app.agents.definition import AgentDefinition
    from hagent_app.ha import HAClient, StateCache
    from hagent_app.tools.registry import HARegistry
    from hagent_app.tools.stores import (
        AgentStateStore,
        EntitySearch,
        HistoryStore,
        KnowledgeStore,
    )


class Args(BaseModel):
    """The base of tool argument models. Unknown arguments fail."""

    model_config = ConfigDict(extra="forbid", frozen=True)


@dataclass(frozen=True, slots=True)
class ToolContext:
    """One tool call's view of its invocation.

    ``agent`` is the definition snapshot that the invocation keeps from
    start to end (design section 9.6). The executor takes the agent ID from
    here, never from the model arguments. With ``dry_run``, the executor
    checks and records a change but does not make it (a test invocation,
    design section 7.2).
    """

    agent: Agent
    invocation_id: str | None = None
    dry_run: bool = False

    @property
    def definition(self) -> AgentDefinition:
        """The agent definition of the snapshot."""
        return self.agent.definition


@dataclass(frozen=True, slots=True)
class ToolEnv:
    """The services that tools can use. It holds no definition writer."""

    ha: HAClient
    states: StateCache
    registry: HARegistry
    knowledge: KnowledgeStore | None = None
    history: HistoryStore | None = None
    agent_state: AgentStateStore | None = None
    entities: EntitySearch | None = None


@dataclass(frozen=True, slots=True)
class Prepared:
    """A request that passed the scope check.

    ``request`` is the JSON form for the audit log. ``payload`` is what
    ``run`` uses. ``services`` and ``entities`` are for the approval check.
    ``previous`` is the previous version of an ``admin`` change.
    """

    request: Mapping[str, Any]
    payload: Any = None
    services: frozenset[str] = frozenset()
    entities: frozenset[str] = frozenset()
    previous: Any = None
    previous_note: str | None = None


@dataclass(frozen=True, slots=True)
class ToolResult:
    """The result of one tool call, for the model."""

    ok: bool
    data: Any = None
    error_code: str | None = None
    error: str | None = None
    audit_id: int | None = None

    def to_data(self) -> dict[str, Any]:
        """The JSON form of the result."""
        if self.ok:
            return {"ok": True, "data": self.data}
        return {"ok": False, "error": {"code": self.error_code, "message": self.error}}


class ToolUnavailableError(Exception):
    """A store that the tool needs is not installed."""


class Tool(ABC):
    """One tool. Subclasses set the class variables and the two steps."""

    name: ClassVar[str]
    group: ClassVar[str]
    description: ClassVar[str]
    args_model: ClassVar[type[Args]]
    # True if the tool changes HA. The executor writes the audit log for it.
    changes_ha: ClassVar[bool] = False
    # True if the tool changes HA or a store. A dry run skips ``run``.
    writes: ClassVar[bool] = False

    def available(self, definition: AgentDefinition) -> bool:
        """True if the definition turns on this tool."""
        return definition.tools.enabled(self.group)

    def level(self, definition: AgentDefinition) -> str:
        """The scope level, for the audit log."""
        if self.group == "ha":
            return definition.tools.ha.level
        return self.group

    @abstractmethod
    async def authorize(self, ctx: ToolContext, env: ToolEnv, args: Any) -> Prepared:
        """Check the scope. Raise ``ScopeDeniedError``. Change nothing."""

    @abstractmethod
    async def run(self, ctx: ToolContext, env: ToolEnv, prepared: Prepared) -> Any:
        """Act on the prepared request. Return JSON data."""


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A tool's description for a model provider."""

    name: str
    group: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)
