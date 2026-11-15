"""The rules component in the router, with the real tools component (task-13)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from hagent_app.agents import Agent
from hagent_app.app import AppContext
from hagent_app.config import Config
from hagent_app.router import InvocationRequest, RouterComponent
from hagent_app.router import store as router_store
from hagent_app.rules import RulesComponent
from hagent_app.tools import StateView, ToolContext, ToolsComponent
from hagent_app.turns.models import TurnInput, TurnRecord, TurnResult
from helpers.tools import Harness, harness
from helpers.wait import wait_until


class _History:
    async def get(self, turn_id: str) -> TurnRecord | None:
        return None


class FakeTurns:
    """Agent turns that answer ``turn`` and count the calls."""

    def __init__(self) -> None:
        self.calls = 0
        self.history = _History()

    async def run_turn(
        self, agent: Agent, turn: TurnInput, *, states: StateView | None = None
    ) -> TurnResult:
        self.calls += 1
        return TurnResult(turn_id=f"t-{turn.invocation_id}", status="ok", result="turn")


class NoProviders:
    """Providers with no network: every decision call fails."""

    @property
    def decisions(self) -> Any:
        msg = "no provider in this test"
        raise RuntimeError(msg)


@dataclass
class Env:
    h: Harness
    tools: ToolsComponent
    rules: RulesComponent
    router: RouterComponent
    turns: FakeTurns


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    async with harness(tmp_path) as h:
        h.ha.time_zone = "Europe/London"
        config = Config(ha_url=h.ha.url, ha_token=h.ha.token, data_dir=tmp_path)
        ctx = AppContext(config, h.db, h.client, h.states, "test-installation")
        tools = ToolsComponent()
        rules = RulesComponent(tools, NoProviders())  # type: ignore[arg-type]
        tools.agent_state = rules.agent_state
        turns = FakeTurns()
        router = RouterComponent(turns, tools=tools, rules=rules)
        await tools.start(ctx)
        await rules.start(ctx)
        await router.start(ctx)
        await wait_until(
            lambda: tools.registry is not None and not tools.registry.stale
        )
        try:
            yield Env(h, tools, rules, router, turns)
        finally:
            await router.stop()
            await rules.stop()
            await tools.stop()


async def _rule(env: Env, agent: Agent, template: str) -> str:
    executor = env.tools.executor()
    ctx = ToolContext(agent, invocation_id="inv-setup")
    created = await executor.execute(
        ctx,
        "self.create",
        {
            "kind": "rule",
            "spec": {
                "name": "probe",
                "match": "{{ true }}",
                "action": {"type": "template", "template": template},
            },
        },
    )
    assert created.ok, created.error
    rule_id = str(created.data["id"])
    enabled = await executor.execute(
        ctx, "self.set_enabled", {"kind": "rule", "item_id": rule_id, "enabled": True}
    )
    assert enabled.ok, enabled.error
    return rule_id


async def test_the_router_answers_with_a_rule(env: Env) -> None:
    agent = await env.h.agent(tools__self=True)
    rule_id = await _rule(env, agent, "{{ now().tzinfo }}")
    result = await env.router.invoke(InvocationRequest(agent.id, "invoke"))
    assert (result.status, result.path) == ("ok", "rule")
    # HA's time zone, read from get_config when the component started.
    assert result.result == "Europe/London"
    assert (result.rule_id, result.rule_version) == (rule_id, 1)
    assert env.turns.calls == 0
    row = await env.h.db.run(lambda c: router_store.get(c, result.invocation_id))
    assert row is not None
    assert (row["path"], row["rule_id"], row["rule_version"]) == ("rule", rule_id, 1)


async def test_the_router_answers_with_a_rule_while_ha_restarts(env: Env) -> None:
    agent = await env.h.agent(
        tools__self=True,
        tools__ha={"level": "read", "scope": {"entities": ["light.hall"]}},
    )
    await _rule(env, agent, "{{ states('light.hall') }}")
    await env.h.ha.restart()
    await wait_until(lambda: env.h.states.stale)
    result = await env.router.invoke(InvocationRequest(agent.id, "invoke"))
    assert (result.status, result.path, result.result) == ("ok", "rule", "off")
    assert env.turns.calls == 0
    env.h.ha.up()


async def test_review_and_assist_skip_the_rules(env: Env) -> None:
    agent = await env.h.agent(tools__self=True)
    await _rule(env, agent, "rule")
    result = await env.router.invoke(InvocationRequest(agent.id, "review"))
    assert (result.path, result.result) == ("agent_turn", "turn")
