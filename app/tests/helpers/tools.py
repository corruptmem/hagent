"""A test harness for the tool executor: FakeHA, the database and fake stores."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hagent_app.agents import Agent, AgentService
from hagent_app.agents.definition import AgentDefinition
from hagent_app.ha import HAClient, StateCache
from hagent_app.http import ChangeEvents
from hagent_app.storage import Database
from hagent_app.tools import AuditLog, HARegistry, ToolContext, ToolExecutor, ToolResult
from hagent_app.tools.base import Prepared, ToolEnv
from hagent_app.tools.stores import StateKind

from helpers.agents import definition
from helpers.fake_ha import FakeHA
from helpers.home import populate
from helpers.wait import wait_until


class FakeKnowledge:
    """A knowledge store that records every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def describe(self, store_ids: Sequence[str]) -> list[dict[str, Any]]:
        self.calls.append(("describe", tuple(store_ids)))
        return [{"id": s, "name": s[:6]} for s in store_ids]

    async def list_items(self, store_id: str) -> list[dict[str, Any]]:
        self.calls.append(("list_items", store_id))
        return []

    async def read(
        self, store_id: str, path: str, section: str | None
    ) -> dict[str, Any]:
        self.calls.append(("read", store_id, path))
        return {"text": ""}

    async def search(
        self, store_ids: Sequence[str], query: str, limit: int, *, agent_id: str
    ) -> list[dict[str, Any]]:
        self.calls.append(("search", tuple(store_ids), query, agent_id))
        return []

    async def write(  # noqa: PLR0913
        self,
        store_id: str,
        path: str,
        mode: str,
        content: str,
        *,
        old_text: str | None,
        base_version: int | None,
        actor_agent_id: str,
    ) -> dict[str, Any]:
        self.calls.append(("write", store_id, path, actor_agent_id))
        return {"version": 1}

    async def delete_file(
        self, store_id: str, path: str, *, base_version: int | None, actor_agent_id: str
    ) -> None:
        self.calls.append(("delete_file", store_id, path, actor_agent_id))


class FakeHistory:
    """A history store that records every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.owners: dict[str, str] = {}

    async def search(
        self, agent_id: str, query: str, filters: Mapping[str, Any], limit: int
    ) -> list[dict[str, Any]]:
        self.calls.append(("search", agent_id, query))
        return []

    async def rule_owner(self, rule_id: str) -> str | None:
        return self.owners.get(rule_id)

    async def rule_stats(self, agent_id: str, rule_id: str) -> dict[str, Any]:
        self.calls.append(("rule_stats", agent_id, rule_id))
        return {"fires": 0}


class FakeAgentState:
    """A store for rules, schedules and subscriptions that records calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.owners: dict[str, str] = {}
        self.specs: list[Mapping[str, Any]] = []

    async def owner(self, kind: StateKind, item_id: str) -> str | None:
        return self.owners.get(item_id)

    async def list_items(self, agent_id: str, kind: StateKind) -> list[dict[str, Any]]:
        self.calls.append(("list_items", agent_id, kind))
        return []

    async def create(
        self, agent_id: str, kind: StateKind, spec: Mapping[str, Any]
    ) -> dict[str, Any]:
        self.calls.append(("create", agent_id, kind))
        self.specs.append(spec)
        return {"id": "new"}

    async def update(
        self, agent_id: str, kind: StateKind, item_id: str, spec: Mapping[str, Any]
    ) -> dict[str, Any]:
        self.calls.append(("update", agent_id, kind, item_id))
        return {"id": item_id}

    async def set_enabled(
        self, agent_id: str, kind: StateKind, item_id: str, enabled: bool
    ) -> None:
        self.calls.append(("set_enabled", agent_id, kind, item_id))

    async def delete(self, agent_id: str, kind: StateKind, item_id: str) -> None:
        self.calls.append(("delete", agent_id, kind, item_id))

    async def reorder_rules(self, agent_id: str, rule_ids: Sequence[str]) -> list[str]:
        self.calls.append(("reorder_rules", agent_id, tuple(rule_ids)))
        return list(rule_ids)

    async def rollback_rule(
        self, agent_id: str, rule_id: str, version: int
    ) -> dict[str, Any]:
        self.calls.append(("rollback_rule", agent_id, rule_id, version))
        return {"id": rule_id, "version": version}

    async def backtest_rule(
        self, agent: Any, rule_id: str, options: Mapping[str, Any]
    ) -> dict[str, Any]:
        self.calls.append(("backtest_rule", agent.id, rule_id))
        return {"id": "b" * 32, "rule_id": rule_id, "cases": []}


class RecordingGate:
    """An approval gate that answers with a fixed value."""

    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.requests: list[str] = []

    async def request(self, ctx: ToolContext, tool: str, prepared: Prepared) -> bool:
        self.requests.append(tool)
        return self.answer


@dataclass
class Harness:
    """Everything that a tool test needs."""

    ha: FakeHA
    client: HAClient
    states: StateCache
    registry: HARegistry
    db: Database
    agents: AgentService
    audit: AuditLog
    executor: ToolExecutor
    knowledge: FakeKnowledge = field(default_factory=FakeKnowledge)
    history: FakeHistory = field(default_factory=FakeHistory)
    agent_state: FakeAgentState = field(default_factory=FakeAgentState)

    async def agent(self, **changes: Any) -> Agent:
        """Create an agent with a definition from ``helpers.agents``."""
        changes.setdefault("name", f"Agent {len(await self.agents.list_agents())}")
        data = definition(**changes)
        return await self.agents.create(AgentDefinition.from_json(json.dumps(data)))

    async def call(
        self,
        agent: Agent,
        tool: str,
        arguments: Mapping[str, Any],
        *,
        dry_run: bool = False,
    ) -> ToolResult:
        """Run one tool call for the agent."""
        ctx = ToolContext(agent, invocation_id="inv-1", dry_run=dry_run)
        return await self.executor.execute(ctx, tool, arguments)

    def store_calls(self) -> list[tuple[Any, ...]]:
        """Every call that reached a fake store."""
        return self.knowledge.calls + self.history.calls + self.agent_state.calls

    def reset(self) -> None:
        """Forget the recorded commands and store calls."""
        self.ha.commands.clear()
        self.knowledge.calls.clear()
        self.history.calls.clear()
        self.agent_state.calls.clear()


@asynccontextmanager
async def harness(tmp_path: Path, **executor_options: Any) -> AsyncIterator[Harness]:
    """Start FakeHA with the sample home, the client, the cache and the registry."""
    async with FakeHA() as ha:
        populate(ha)
        client = HAClient(ha.url, ha.token, backoff_initial=0.05, backoff_max=0.1)
        states = StateCache(client)
        registry = HARegistry(client)
        await states.start()
        await registry.start()
        await client.start()
        db = await Database.open(tmp_path / "hagent.db")
        try:
            await wait_until(lambda: states.ready and not registry.stale)
            knowledge, history, agent_state = (
                FakeKnowledge(),
                FakeHistory(),
                FakeAgentState(),
            )
            env = ToolEnv(
                ha=client,
                states=states,
                registry=registry,
                knowledge=knowledge,
                history=history,
                agent_state=agent_state,
            )
            audit = AuditLog(db)
            executor = ToolExecutor(env, audit, **executor_options)
            yield Harness(
                ha,
                client,
                states,
                registry,
                db,
                AgentService(db, ChangeEvents()),
                audit,
                executor,
                knowledge,
                history,
                agent_state,
            )
        finally:
            await registry.stop()
            await states.stop()
            await client.stop()
            await db.close()
