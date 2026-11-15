"""Tests of the rule engine (task-13, design section 10)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from hagent_app.agents import Agent
from hagent_app.agents.definition import AgentDefinition
from hagent_app.providers.decisions import (
    ChoiceAnswer,
    ChoiceQuestion,
    DecisionRequest,
    DecisionResult,
    Refusal,
    YesNoAnswer,
    YesNoQuestion,
)
from hagent_app.providers.errors import ProviderHTTPError
from hagent_app.providers.prices import TokenUsage
from hagent_app.router import store as router_store
from hagent_app.router.models import InvocationRequest, InvocationResult
from hagent_app.router.retry import RetryPolicy
from hagent_app.router.rules import RuleOutcome, RuleRequest
from hagent_app.rules import AgentStateStores, RuleEngine, RuleStore
from hagent_app.rules.context import RuleFailure
from hagent_app.rules.spec import HistoryItem
from hagent_app.tools import ToolContext, ToolExecutor
from hagent_app.tools.scope import ScopeDeniedError
from hagent_app.tools.stores import StoreError
from hagent_app.turns.history import TurnHistory
from helpers.tools import Harness, harness
from helpers.wait import wait_until

READ_HALL = {"tools__ha": {"level": "read", "scope": {"entities": ["light.hall"]}}}
CONTROL_KITCHEN = {"tools__ha": {"level": "control", "scope": {"areas": ["kitchen"]}}}
DECISION = {"provider": "openai", "model": "gpt-6-luna"}
MODELS = {"chat": {"provider": "openai", "model": "gpt-5-mini"}, "decision": DECISION}


class FakeDecisions:
    """A decision client that records each request."""

    def __init__(self) -> None:
        self.requests: list[DecisionRequest] = []
        self.error: Exception | None = None
        self.answers: dict[str, Any] = {}

    async def decide(
        self, request: DecisionRequest, *, agent_id: str | None
    ) -> DecisionResult:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return DecisionResult("gpt-6-luna", dict(self.answers), TokenUsage())


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def _no_sleep(_delay: float) -> None:
    return None


@dataclass
class Env:
    h: Harness
    store: RuleStore
    executor: ToolExecutor
    engine: RuleEngine
    decisions: FakeDecisions
    clock: Clock
    count: int = 0
    outcomes: list[RuleOutcome] = field(default_factory=list)

    async def agent(self, **changes: Any) -> Agent:
        changes.setdefault("tools__self", True)
        return await self.h.agent(**changes)

    async def edit(self, agent: Agent, **changes: Any) -> Agent:
        data = agent.definition.to_data()
        data.update(changes)
        return await self.h.agents.update(
            agent.id, AgentDefinition.from_json(json.dumps(data))
        )

    async def call(self, agent: Agent, tool: str, args: Mapping[str, Any]) -> Any:
        ctx = ToolContext(agent, invocation_id="inv-tool")
        result = await self.executor.execute(ctx, tool, args)
        assert result.ok, (result.error_code, result.error)
        return result.data

    async def rule(self, agent: Agent, *, activate: bool = True, **parts: Any) -> str:
        """Create a rule with the self tools, and turn it on."""
        created = await self.call(agent, "self.create", rule_args(**parts))
        if activate:
            await self.call(
                agent,
                "self.set_enabled",
                {"kind": "rule", "item_id": created["id"], "enabled": True},
            )
        return str(created["id"])

    async def invoke(
        self,
        agent: Agent,
        data: Mapping[str, Any] | None = None,
        inputs: Mapping[str, Any] | None = None,
        source: str = "invoke",
    ) -> RuleOutcome:
        self.count += 1
        invocation_id = f"inv-{self.count}"
        request = InvocationRequest(agent.id, source, data=data or {})
        await self.h.db.run(
            lambda c: router_store.insert(c, invocation_id, request, status="running")
        )
        outcome = await self.engine.try_rules(
            RuleRequest(invocation_id, agent, request, inputs or {})
        )
        self.outcomes.append(outcome)
        return outcome


def rule_args(**parts: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "name": "probe",
        "match": "{{ true }}",
        "action": {"type": "template", "template": "on"},
    }
    spec.update(parts)
    return {"kind": "rule", "spec": spec}


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    async with harness(tmp_path) as h:
        store = RuleStore()
        store.bind(h.db)
        executor = ToolExecutor(
            replace(h.executor.env, agent_state=AgentStateStores({"rule": store})),
            h.audit,
        )
        decisions = FakeDecisions()
        clock = Clock()
        engine = RuleEngine(
            store,
            h.db,
            lambda: executor,
            lambda: decisions,
            retry=RetryPolicy(attempts=2, base_delay=0),
            sleep=_no_sleep,
            monotonic=clock,
        )
        yield Env(h, store, executor, engine, decisions, clock)


# ------------------------------------------------------- AC1: state scope


async def test_templates_cannot_read_entities_outside_the_scope(env: Env) -> None:
    await env.h.ha.set_state("light.hall", "on", {"friendly_name": "Hall"})
    await env.h.ha.set_state("lock.front", "unlocked", {"battery": 80})
    await wait_until(lambda: env.h.states.get("lock.front").state == "unlocked")  # type: ignore[union-attr]
    agent = await env.agent(**READ_HALL)
    template = (
        "{{ states('light.hall') }}|{{ states('lock.front') }}"
        "|{{ states.lock.front }}|{{ states.lock | list | count }}"
        "|{{ is_state('lock.front', 'unlocked') }}"
        "|{{ state_attr('lock.front', 'battery') }}"
        "|{{ has_value('lock.front') }}"
        "|{{ states | map(attribute='entity_id') | list }}"
        "|{{ states.light.hall.name }}"
    )
    await env.rule(agent, action={"type": "template", "template": template})
    outcome = await env.invoke(agent)
    assert outcome.answered, outcome
    assert outcome.result == ("on|unknown|None|0|False|None|False|['light.hall']|Hall")


async def test_an_agent_with_no_ha_access_sees_only_its_inputs(env: Env) -> None:
    agent = await env.agent(inputs__entities=["sensor.lounge_temp"])
    template = "{{ states | map(attribute='entity_id') | list }}"
    await env.rule(agent, action={"type": "template", "template": template})
    outcome = await env.invoke(agent)
    assert outcome.result == ["sensor.lounge_temp"]


async def test_templates_never_see_the_state_context(env: Env) -> None:
    agent = await env.agent(**READ_HALL)
    template = "{{ states.light.hall.context }}{{ states.light.hall.__class__ }}"
    await env.rule(agent, action={"type": "template", "template": template})
    outcome = await env.invoke(agent)
    assert outcome.answered
    assert outcome.result == ""


async def test_an_area_scope_uses_the_registry(env: Env) -> None:
    agent = await env.agent(**CONTROL_KITCHEN)
    template = "{{ states | map(attribute='entity_id') | list | sort }}"
    await env.rule(agent, action={"type": "template", "template": template})
    outcome = await env.invoke(agent)
    # The config entity number.kitchen_level is not an area target, as in HA.
    assert outcome.result == ["light.kitchen", "switch.kettle"]


# ---------------------------------------------- template action and writes


async def test_a_template_rule_writes_through_the_executor(env: Env) -> None:
    agent = await env.agent(outputs=["input_number.flow"])
    rule_id = await env.rule(
        agent,
        match="{{ data.room == 'lounge' }}",
        action={"type": "template", "template": "{{ data.temp | float + 1.5 }}"},
        result="{{ output * 2 }}",
        writes=[
            {
                "service": "input_number.set_value",
                "entity_id": "input_number.flow",
                "data": {"value": "{{ result }}"},
            }
        ],
    )
    env.h.reset()
    assert not (await env.invoke(agent, {"room": "hall", "temp": 20})).answered
    assert env.h.ha.commands_of("call_service") == []
    outcome = await env.invoke(agent, {"room": "lounge", "temp": "20"})
    assert outcome.answered
    assert outcome.result == 43.0
    assert (outcome.rule_id, outcome.rule_version) == (rule_id, 1)
    [command] = env.h.ha.commands_of("call_service")
    assert command["service"] == "set_value"
    assert command["target"] == {"entity_id": ["input_number.flow"]}
    assert command["service_data"] == {"value": 43.0}
    # The write has its audit row, as an agent turn call has.
    entry = (await env.h.audit.entries(agent.id))[0]
    assert entry.tool == "ha.call_service"
    assert entry.status == "ok"
    stats = await env.store.rule_stats(agent.id, rule_id)
    assert stats["versions"][0]["matched"] == 1
    assert stats["versions"][0]["answered"] == 1


async def test_a_write_that_needs_approval_never_reaches_ha(env: Env) -> None:
    agent = await env.agent(outputs=["input_number.flow"])
    rule_id = await env.rule(
        agent,
        writes=[
            {
                "service": "input_number.set_value",
                "entity_id": "input_number.flow",
                "data": {"value": "1"},
            }
        ],
    )
    # The user later marks the output for approval.
    agent = await env.edit(agent, approval={"entities": ["input_number.flow"]})
    env.h.reset()
    outcome = await env.invoke(agent)
    assert not outcome.answered
    assert outcome.reason == "rule_error"
    assert env.h.ha.commands_of("call_service") == []
    stats = await env.store.rule_stats(agent.id, rule_id)
    assert stats["versions"][0]["errors"] == 1
    assert "approval_required" in stats["versions"][0]["last_error"]


async def test_the_rule_executor_denies_approval_calls_without_a_gate(
    env: Env,
) -> None:
    """The second barrier: the executor of the rule stage has no gate."""
    agent = await env.agent(**CONTROL_KITCHEN, approval__services=["light.turn_on"])
    env.h.reset()
    result = await env.executor.without_approval().execute(
        ToolContext(agent, invocation_id="inv-x"),
        "ha.call_service",
        {
            "domain": "light",
            "service": "turn_on",
            "target": {"entity_id": ["light.kitchen"]},
        },
    )
    assert result.error_code == "approval_unavailable"
    assert env.h.ha.commands_of("call_service") == []
    assert (await env.h.audit.entries(agent.id))[0].decision == "denied"


async def test_a_write_outside_a_shrunk_scope_never_reaches_ha(env: Env) -> None:
    agent = await env.agent(**CONTROL_KITCHEN)
    await env.rule(
        agent, writes=[{"service": "light.turn_on", "entity_id": "light.kitchen"}]
    )
    agent = await env.edit(
        agent, tools={"self": True, "ha": {"level": "read", "scope": {"all": True}}}
    )
    env.h.reset()
    outcome = await env.invoke(agent)
    assert outcome.reason == "rule_error"
    assert env.h.ha.commands_of("call_service") == []


async def test_rendered_write_data_is_checked_again(env: Env) -> None:
    """A data template that renders an entity ID outside the scope fails."""
    agent = await env.agent(**CONTROL_KITCHEN)
    await env.rule(
        agent,
        writes=[
            {
                "service": "light.turn_on",
                "entity_id": "light.kitchen",
                "data": {"transition": "{{ 'lock.front' }}"},
            }
        ],
    )
    env.h.reset()
    outcome = await env.invoke(agent)
    assert outcome.reason == "rule_error"
    assert env.h.ha.commands_of("call_service") == []
    entry = (await env.h.audit.entries(agent.id))[0]
    assert entry.decision == "denied"
    assert entry.reason_code == "out_of_scope"


# ---------------------------------------------------- order and decline


async def test_the_first_matching_rule_answers_and_a_decline_stops(
    env: Env,
) -> None:
    agent = await env.agent()
    first = await env.rule(
        agent,
        name="first",
        match="{{ data.x > 1 }}",
        action={"type": "template", "template": "first"},
        result="{{ 'decline' if data.x > 5 else output }}",
    )
    second = await env.rule(
        agent, name="second", action={"type": "template", "template": "second"}
    )
    assert (await env.invoke(agent, {"x": 0})).result == "second"
    assert (await env.invoke(agent, {"x": 2})).result == "first"
    declined = await env.invoke(agent, {"x": 9})
    # A decline goes to the agent turn, not to the next rule.
    assert not declined.answered
    assert (declined.reason, declined.rule_id) == ("declined", first)
    await env.call(agent, "self.reorder_rules", {"rule_ids": [second, first]})
    assert (await env.invoke(agent, {"x": 2})).result == "second"


async def test_a_rule_with_a_template_error_records_the_error(env: Env) -> None:
    agent = await env.agent()
    rule_id = await env.rule(
        agent, action={"type": "template", "template": "{{ 'x' | float }}"}
    )
    outcome = await env.invoke(agent)
    assert (outcome.answered, outcome.reason) == (False, "rule_error")
    stats = await env.store.rule_stats(agent.id, rule_id)
    assert "no default" in stats["versions"][0]["last_error"]


async def test_reorder_needs_each_rule_exactly_once(env: Env) -> None:
    agent = await env.agent()
    one = await env.rule(agent)
    await env.rule(agent)
    with pytest.raises(StoreError, match="exactly once"):
        await env.store.reorder(agent.id, [one])
    other = await env.agent(name="Other")
    with pytest.raises(StoreError):
        await env.store.reorder(other.id, [one])


# ------------------------------------------------ AC6: versions, rollback


async def test_each_change_creates_a_version_with_its_own_statistics(
    env: Env,
) -> None:
    agent = await env.agent()
    rule_id = await env.rule(agent, action={"type": "template", "template": "v1"})
    assert (await env.invoke(agent)).result == "v1"
    updated = await env.call(
        agent,
        "self.update",
        {
            **rule_args(action={"type": "template", "template": "v2"}),
            "item_id": rule_id,
        },
    )
    assert updated == {"id": rule_id, "version": 2, "state": "draft"}
    # A new version starts as a draft: it does not answer yet.
    assert not (await env.invoke(agent)).answered
    await env.call(
        agent, "self.set_enabled", {"kind": "rule", "item_id": rule_id, "enabled": True}
    )
    outcome = await env.invoke(agent)
    assert (outcome.result, outcome.rule_version) == ("v2", 2)
    await env.invoke(agent)

    rolled = await env.call(
        agent, "self.rollback_rule", {"rule_id": rule_id, "version": 1}
    )
    assert rolled == {"id": rule_id, "version": 1, "state": "active"}
    outcome = await env.invoke(agent)
    assert (outcome.result, outcome.rule_version) == ("v1", 1)

    stats = await env.store.rule_stats(agent.id, rule_id)
    assert stats["current_version"] == 1
    assert stats["latest_version"] == 2
    by_version = {v["version"]: v for v in stats["versions"]}
    assert by_version[1]["answered"] == 2
    assert by_version[2]["answered"] == 2
    # The spec of version 1 never changed.
    spec = await env.store.get_spec(agent.id, rule_id, 1)
    assert spec is not None
    assert spec["action"]["template"] == "v1"


async def test_rollback_to_an_unknown_version_fails(env: Env) -> None:
    agent = await env.agent()
    rule_id = await env.rule(agent)
    result = await env.executor.execute(
        ToolContext(agent, invocation_id="inv"),
        "self.rollback_rule",
        {"rule_id": rule_id, "version": 7},
    )
    assert result.error_code == "unknown_version"


async def test_turn_history_reads_the_rule_statistics(env: Env) -> None:
    agent = await env.agent()
    rule_id = await env.rule(agent)
    await env.invoke(agent)
    history = TurnHistory(env.h.db)
    assert await history.rule_owner(rule_id) is None
    history.rules = env.store
    assert await history.rule_owner(rule_id) == agent.id
    stats = await history.rule_stats(agent.id, rule_id)
    assert stats["versions"][0]["answered"] == 1
    # Another agent reads nothing.
    assert await history.rule_stats("other", rule_id) == {}


async def test_deleted_rules_do_not_answer(env: Env) -> None:
    agent = await env.agent()
    rule_id = await env.rule(agent)
    await env.call(agent, "self.delete", {"kind": "rule", "item_id": rule_id})
    assert not (await env.invoke(agent)).answered
    assert await env.store.owner("rule", rule_id) is None
    assert await env.store.rule_owner(rule_id) == agent.id


# ------------------------------------------------------- the result cache


async def test_the_cache_returns_the_previous_result(env: Env) -> None:
    agent = await env.agent(outputs=["input_number.flow"])
    rule_id = await env.rule(
        agent,
        action={"type": "template", "template": "{{ data.n }}"},
        writes=[
            {
                "service": "input_number.set_value",
                "entity_id": "input_number.flow",
                "data": {"value": "{{ result }}"},
            }
        ],
        cache={"seconds": 60, "keys": ["data.room"]},
    )
    env.h.reset()
    assert (await env.invoke(agent, {"room": "a", "n": 1})).result == 1
    # Same key value: the previous result, no action, no write.
    assert (await env.invoke(agent, {"room": "a", "n": 2})).result == 1
    assert len(env.h.ha.commands_of("call_service")) == 1
    assert (await env.invoke(agent, {"room": "b", "n": 3})).result == 3
    env.clock.now += 61
    assert (await env.invoke(agent, {"room": "a", "n": 4})).result == 4
    stats = await env.store.rule_stats(agent.id, rule_id)
    assert stats["versions"][0]["cache_hits"] == 1
    assert stats["versions"][0]["answered"] == 4


# ---------------------------------------------------- AC4: context items


async def test_history_context_respects_scope_and_cache_time(env: Env) -> None:
    agent = await env.agent(
        tools__ha={"level": "read", "scope": {"entities": ["sensor.lounge_temp"]}}
    )
    await env.rule(
        agent,
        context=[
            {
                "type": "history",
                "name": "temps",
                "entity_ids": ["sensor.lounge_temp"],
                "hours": 24,
                "cache_seconds": 300,
            }
        ],
        action={
            "type": "template",
            "template": "{{ context.temps['sensor.lounge_temp'][0].mean }}",
        },
    )
    env.h.reset()
    assert (await env.invoke(agent)).result == 1.0
    [command] = env.h.ha.commands_of("recorder/statistics_during_period")
    assert command["statistic_ids"] == ["sensor.lounge_temp"]
    assert command["period"] == "hour"
    await env.invoke(agent)
    assert len(env.h.ha.commands_of("recorder/statistics_during_period")) == 1
    env.clock.now += 301
    await env.invoke(agent)
    assert len(env.h.ha.commands_of("recorder/statistics_during_period")) == 2


async def test_a_cached_context_item_is_checked_against_the_scope(env: Env) -> None:
    agent = await env.agent(
        tools__ha={"level": "read", "scope": {"entities": ["sensor.lounge_temp"]}}
    )
    item = HistoryItem(
        type="history",
        name="temps",
        entity_ids=["sensor.lounge_temp"],
        cache_seconds=300,
    )
    ctx = ToolContext(agent, invocation_id="inv-1")
    await env.engine.context.collect(ctx, env.executor, [item], "invoke")
    # The same definition version, but a scope without the entity: the
    # cached value must not pass.
    data = agent.definition.to_data()
    data["tools"]["ha"]["scope"] = {"entities": ["light.hall"]}
    narrowed = replace(agent, definition=AgentDefinition.from_json(json.dumps(data)))
    env.h.reset()
    with pytest.raises(ScopeDeniedError) as err:
        await env.engine.context.collect(
            ToolContext(narrowed, invocation_id="inv-2"), env.executor, [item], "invoke"
        )
    assert err.value.code == "out_of_scope"
    assert env.h.ha.ha_commands() == []


async def test_an_out_of_scope_history_item_never_reaches_ha(env: Env) -> None:
    agent = await env.agent(**READ_HALL)
    item = HistoryItem(type="history", name="h", entity_ids=["lock.front"])
    env.h.reset()
    with pytest.raises(RuleFailure, match="out_of_scope"):
        await env.engine.context.collect(
            ToolContext(agent, invocation_id="inv"), env.executor, [item], "invoke"
        )
    assert env.h.ha.ha_commands() == []


async def test_action_response_context_calls_a_data_action(env: Env) -> None:
    calls: list[dict[str, Any]] = []

    def forecast(_ha: Any, msg: dict[str, Any]) -> dict[str, Any]:
        calls.append(msg)
        return {
            "context": {"id": "c1"},
            "response": {"weather.home": {"forecast": [{"temperature": 4}]}},
        }

    env.h.ha.handlers["call_service"] = forecast
    agent = await env.agent(
        tools__ha={"level": "control", "scope": {"entities": ["weather.home"]}}
    )
    item = {
        "type": "action_response",
        "name": "forecast",
        "service": "weather.get_forecasts",
        "target": {"entity_id": "weather.home"},
        "data": {"type": "hourly"},
    }
    await env.rule(
        agent,
        context=[item],
        action={
            "type": "template",
            "template": (
                "{{ context.forecast['weather.home'].forecast[0].temperature }}"
            ),
        },
    )
    assert (await env.invoke(agent)).result == 4
    [call] = calls
    assert call["return_response"] is True
    assert call["target"] == {"entity_id": ["weather.home"]}
    # The scope shrinks: the next run never calls HA.
    agent = await env.edit(
        agent,
        tools={
            "self": True,
            "ha": {"level": "control", "scope": {"entities": ["light.hall"]}},
        },
    )
    outcome = await env.invoke(agent)
    assert outcome.reason == "rule_error"
    assert len(calls) == 1


async def test_store_file_context_needs_an_attached_store(env: Env) -> None:
    agent = await env.agent()
    assert agent.private_store is not None
    store_id = agent.private_store.id
    await env.rule(
        agent,
        context=[
            {
                "type": "store_file",
                "name": "notes",
                "store_id": store_id,
                "path": "a.md",
            }
        ],
        action={"type": "template", "template": "{{ context.notes.text == '' }}"},
    )
    env.h.reset()
    assert (await env.invoke(agent)).result is True
    assert env.h.knowledge.calls == [("read", store_id, "a.md")]
    # The user turns the knowledge tools off: the rule fails, reads nothing.
    agent = await env.edit(agent, tools={"self": True, "knowledge": False})
    env.h.reset()
    outcome = await env.invoke(agent)
    assert outcome.reason == "rule_error"
    assert env.h.knowledge.calls == []


async def test_invocation_history_reads_own_invocations_only(env: Env) -> None:
    agent = await env.agent()
    other = await env.agent(name="Other")
    for owner, source, value in (
        (agent, "invoke", "mine"),
        (agent, "schedule", "other source"),
        (other, "invoke", "other agent"),
    ):
        request = InvocationRequest(owner.id, source, data={"v": value})
        invocation_id = f"old-{value}"
        result = InvocationResult(
            invocation_id, owner.id, "ok", path="agent_turn", result=value
        )

        def write(
            conn: sqlite3.Connection,
            request: InvocationRequest = request,
            result: InvocationResult = result,
        ) -> None:
            router_store.insert(conn, result.invocation_id, request, status="running")
            router_store.finish(conn, result)

        await env.h.db.run(write)
    await env.rule(
        agent,
        context=[{"type": "invocation_history", "name": "recent", "limit": 5}],
        action={
            "type": "template",
            "template": "{{ context.recent | map(attribute='result') | list }}",
        },
    )
    assert (await env.invoke(agent)).result == ["mine"]


# -------------------------------------------------- AC3: decision action


async def test_a_decision_rule_sends_one_request_with_all_questions(
    env: Env,
) -> None:
    env.decisions.answers = {
        "cold": YesNoAnswer("cold", 0.8),
        "mode": ChoiceAnswer("mode", "eco", {"eco": 0.7, "boost": 0.3}, 0.6),
    }
    agent = await env.agent(
        models=MODELS,
        tools__ha={"level": "read", "scope": {"entities": ["sensor.lounge_temp"]}},
    )
    rule_id = await env.rule(
        agent,
        context=[{"type": "invocation_history", "name": "recent"}],
        action={
            "type": "decision",
            "questions": [
                {
                    "type": "yes_no",
                    "name": "cold",
                    "text": "Is it cold?",
                    "yes": "Cold",
                },
                {
                    "type": "choice",
                    "name": "mode",
                    "text": "Which mode?",
                    "options": {"eco": "Save energy", "boost": "Heat fast"},
                },
            ],
            "include": {"inputs": True, "data": ["room"], "context": True},
        },
        result=(
            "{{ 'decline' if answers.cold.probability < 0.5 else answers.mode.choice }}"
        ),
    )
    outcome = await env.invoke(
        agent, {"room": "lounge", "secret": "x"}, inputs={"sensor.lounge_temp": "17"}
    )
    assert outcome.answered, outcome
    assert outcome.result == "eco"
    [request] = env.decisions.requests
    assert request.model.model == "gpt-6-luna"
    assert request.values == {
        "inputs": {"sensor.lounge_temp": "17"},
        "data": {"room": "lounge"},
        "context": {"recent": []},
    }
    first, second = request.questions
    assert first == YesNoQuestion("cold", "Is it cold?", "Cold", None)
    assert isinstance(second, ChoiceQuestion)
    assert dict(second.options) == {"eco": "Save energy", "boost": "Heat fast"}

    env.decisions.answers["cold"] = YesNoAnswer("cold", 0.1)
    declined = await env.invoke(agent, {"room": "lounge"})
    assert (declined.answered, declined.reason) == (False, "declined")
    stats = await env.store.rule_stats(agent.id, rule_id)
    assert stats["versions"][0]["declined"] == 1


async def test_a_refused_question_is_marked(env: Env) -> None:
    env.decisions.answers = {"cold": Refusal("cold")}
    agent = await env.agent(models=MODELS)
    await env.rule(
        agent,
        action={
            "type": "decision",
            "questions": [{"type": "yes_no", "name": "cold", "text": "Cold?"}],
        },
        result="{{ answers.cold.refused }}",
    )
    assert (await env.invoke(agent)).result is True


async def test_a_failing_decision_call_retries_then_declines(env: Env) -> None:
    env.decisions.error = ProviderHTTPError("connection failed")
    agent = await env.agent(models=MODELS)
    rule_id = await env.rule(
        agent,
        action={
            "type": "decision",
            "questions": [{"type": "yes_no", "name": "cold", "text": "Cold?"}],
        },
    )
    outcome = await env.invoke(agent)
    assert (outcome.answered, outcome.reason) == (False, "declined")
    assert len(env.decisions.requests) == 2
    stats = await env.store.rule_stats(agent.id, rule_id)
    assert stats["versions"][0]["declined"] == 1


async def test_rule_cost_is_recorded_for_the_version(env: Env) -> None:
    """The cost of the provider calls of the rule run goes to its version."""
    agent = await env.agent(models=MODELS)
    env.decisions.answers = {"cold": YesNoAnswer("cold", 0.9)}
    rule_id = await env.rule(
        agent,
        action={
            "type": "decision",
            "questions": [{"type": "yes_no", "name": "cold", "text": "Cold?"}],
        },
    )
    original = env.decisions.decide

    async def metered(request: DecisionRequest, *, agent_id: str | None) -> Any:
        invocation_id = f"inv-{env.count}"
        await env.h.db.run(
            lambda c: c.execute(
                "INSERT INTO provider_calls (started_at, finished_at, budget_day,"
                " agent_id, provider, call_type, model, status, cost_pusd,"
                " cost_source, invocation_id) VALUES ('2026-01-01', '2026-01-01',"
                " '2026-01-01', ?, 'openai', 'decision', 'gpt-6-luna', 'ok',"
                " 5000000, 'price_table', ?)",
                (agent_id, invocation_id),
            )
        )
        return await original(request, agent_id=agent_id)

    env.decisions.decide = metered  # type: ignore[method-assign]
    assert (await env.invoke(agent)).answered
    stats = await env.store.rule_stats(agent.id, rule_id)
    assert Decimal(str(stats["versions"][0]["cost_usd"])) == Decimal("0.000005")


# ------------------------------------- AC7: HA restart and internet down


async def test_template_rules_work_while_ha_restarts_and_offline(env: Env) -> None:
    env.decisions.error = ProviderHTTPError("no internet")
    agent = await env.agent(**READ_HALL)
    await env.h.ha.set_state("light.hall", "on")
    await wait_until(lambda: env.h.states.get("light.hall").state == "on")  # type: ignore[union-attr]
    await env.rule(
        agent, action={"type": "template", "template": "{{ states('light.hall') }}"}
    )
    await env.h.ha.restart()
    await wait_until(lambda: env.h.states.stale and env.h.registry.stale)
    outcome = await env.invoke(agent)
    assert outcome.answered, outcome
    assert outcome.result == "on"
    assert env.decisions.requests == []
    env.h.ha.up()


async def test_an_area_scope_uses_the_last_registry_while_ha_restarts(
    env: Env,
) -> None:
    agent = await env.agent(**CONTROL_KITCHEN)
    await env.rule(
        agent,
        action={
            "type": "template",
            "template": "{{ states('switch.kettle') }}|{{ states('light.hall') }}",
        },
    )
    await env.h.ha.restart()
    await wait_until(lambda: env.h.registry.stale)
    assert (await env.invoke(agent)).result == "off|unknown"
    env.h.ha.up()


async def test_writes_fail_closed_while_ha_restarts(env: Env) -> None:
    agent = await env.agent(**CONTROL_KITCHEN)
    await env.rule(
        agent, writes=[{"service": "light.turn_on", "entity_id": "light.kitchen"}]
    )
    await env.h.ha.restart()
    await wait_until(lambda: env.h.states.stale)
    outcome = await env.invoke(agent)
    assert outcome.reason == "rule_error"
    env.h.ha.up()


class RecordingObserver:
    """A ``ServiceCallObserver`` that records each call."""

    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    def call_started(self, agent_id: str) -> int:
        self.events.append(("started", agent_id))
        return len(self.events)

    def call_finished(self, token: int, context_id: str | None) -> None:
        self.events.append(("finished", context_id))


async def test_rule_writes_reach_the_own_write_observer(env: Env) -> None:
    """Rule writes use the same path as agent calls: ToolEnv.calls sees them,
    so the scheduler can filter the agent's own writes (task-15)."""
    observer = RecordingObserver()
    env.executor = ToolExecutor(replace(env.executor.env, calls=observer), env.h.audit)
    engine = RuleEngine(
        env.store, env.h.db, lambda: env.executor, lambda: env.decisions
    )
    agent = await env.agent(**CONTROL_KITCHEN)
    await env.rule(
        agent, writes=[{"service": "light.turn_on", "entity_id": "light.kitchen"}]
    )
    request = InvocationRequest(agent.id, "invoke")
    await env.h.db.run(
        lambda c: router_store.insert(c, "inv-obs", request, status="running")
    )
    outcome = await engine.try_rules(RuleRequest("inv-obs", agent, request, {}))
    assert outcome.answered, outcome
    assert [e[0] for e in observer.events] == ["started", "finished"]
    assert observer.events[0] == ("started", agent.id)
    assert observer.events[1][1] is not None
