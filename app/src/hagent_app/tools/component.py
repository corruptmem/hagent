"""The tools component: the registry, the scope monitor and the executor."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from hagent_app.agents import AgentReader
from hagent_app.http.events import ChangeEvents
from hagent_app.tools.audit import AuditLog
from hagent_app.tools.base import ToolEnv
from hagent_app.tools.executor import ApprovalGate, ToolExecutor
from hagent_app.tools.monitor import ScopeMonitor
from hagent_app.tools.registry import HARegistry
from hagent_app.tools.stores import (
    AgentStateStore,
    EntitySearch,
    HistoryStore,
    KnowledgeStore,
)

if TYPE_CHECKING:
    from hagent_app.app import AppContext


class ToolsUnavailableError(RuntimeError):
    """The tools component has not started."""


class ToolsComponent:
    """Owns the tool executor. Later tasks plug in their stores here.

    Task-10 calls ``executor()`` in each agent turn. Task-11, task-10 and
    task-13 to task-15 set ``knowledge``, ``history`` and ``agent_state``
    before the app starts. Task-11 also sets ``entities``, the ranked entity
    search of ``ha.find_entities``. Task-16 sets ``approval``.
    """

    name = "tools"

    def __init__(self, events: ChangeEvents | None = None) -> None:
        self._events = events
        self.knowledge: KnowledgeStore | None = None
        self.history: HistoryStore | None = None
        self.agent_state: AgentStateStore | None = None
        self.entities: EntitySearch | None = None
        self.approval: ApprovalGate | None = None
        self.registry: HARegistry | None = None
        self.audit: AuditLog | None = None
        self.monitor: ScopeMonitor | None = None
        self._executor: ToolExecutor | None = None

    def executor(self) -> ToolExecutor:
        """The running executor. Raise before start."""
        if self._executor is None:
            msg = "the tools component is not running"
            raise ToolsUnavailableError(msg)
        return self._executor

    async def start(self, ctx: AppContext) -> None:
        """Start the registry and the monitor, and build the executor."""
        self.registry = HARegistry(ctx.ha)
        self.audit = AuditLog(ctx.db)
        self.monitor = ScopeMonitor(
            ctx.db,
            AgentReader(ctx.db),
            ctx.ha,
            self._events,
            known_entities=_StateIds(ctx),
        )
        self.registry.add_reload_listener(self.monitor.on_reload)
        env = ToolEnv(
            ha=ctx.ha,
            states=ctx.states,
            registry=self.registry,
            knowledge=self.knowledge,
            history=self.history,
            agent_state=self.agent_state,
            entities=self.entities,
        )
        self._executor = ToolExecutor(env, self.audit, approval=self.approval)
        await self.registry.start()

    async def stop(self) -> None:
        """Stop the registry."""
        self._executor = None
        if self.registry is not None:
            await self.registry.stop()
            self.registry = None


class _StateIds:
    """An iterable view of the entity IDs in the state cache."""

    def __init__(self, ctx: AppContext) -> None:
        self._states = ctx.states

    def __iter__(self) -> Iterator[str]:
        return (state.entity_id for state in self._states)
