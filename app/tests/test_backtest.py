"""Tests of backtests, trials and invocation records (task-14, design 10.7)."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer
from hagent_app.agents import Agent
from hagent_app.app import AppContext
from hagent_app.backtest import BacktestComponent, BacktestOptions
from hagent_app.backtest import records as backtest_records
from hagent_app.backtest.runner import BacktestError
from hagent_app.config import Config
from hagent_app.http import HttpServer
from hagent_app.providers.decisions import (
    DecisionRequest,
    DecisionResult,
    YesNoAnswer,
)
from hagent_app.providers.errors import BudgetExceededError
from hagent_app.providers.meter import BudgetState, current_invocation
from hagent_app.providers.prices import TokenUsage
from hagent_app.router import InvocationRequest, RouterComponent
from hagent_app.router.models import InvocationResult
from hagent_app.rules import RulesComponent
from hagent_app.rules.render_pool import shared_pool
from hagent_app.tools import StateView, ToolContext, ToolsComponent
from hagent_app.turns.history import TurnHistory
from hagent_app.turns.models import TurnInput, TurnRecord, TurnResult
from helpers.tools import Harness, harness
from helpers.wait import wait_until

TEMP = "sensor.lounge_temp"
FLOW = "input_number.flow"
DECISION = {"provider": "openai", "model": "gpt-6-luna"}
YES_NO = {
    "type": "decision",
    "questions": [{"type": "yes_no", "name": "cold", "text": "Is it cold?"}],
}


# ------------------------------------------------------------------ fakes


class _History:
    async def get(self, turn_id: str) -> TurnRecord | None:
        return None


class FakeTurns:
    """Agent turns that answer with ``answer(turn)``.

    With ``write``, the turn calls ``ha.call_service`` on the output, as a
    model would. A dry-run turn must not reach HA.
    """

    def __init__(self, tools: ToolsComponent) -> None:
        self.tools = tools
        self.turns: list[TurnInput] = []
        self.history = _History()
        self.answer: Callable[[TurnInput], Any] = lambda _t: "turn"
        self.write = False
        self.gate: asyncio.Event | None = None
        self.write_results: list[Any] = []

    async def run_turn(
        self, agent: Agent, turn: TurnInput, *, states: StateView | None = None
    ) -> TurnResult:
        self.turns.append(turn)
        if self.write:
            ctx = ToolContext(
                agent, invocation_id=turn.invocation_id, dry_run=turn.dry_run
            )
            result = await self.tools.executor().execute(
                ctx,
                "ha.call_service",
                {
                    "domain": "input_number",
                    "service": "set_value",
                    "target": {"entity_id": [FLOW]},
                    "data": {"value": 40},
                },
            )
            self.write_results.append(result)
        if self.gate is not None:
            await self.gate.wait()
        return TurnResult(
            turn_id=f"t-{turn.invocation_id}", status="ok", result=self.answer(turn)
        )


class FakeDecisions:
    """A decision client that answers ``probability`` and records a cost row.

    The cost row carries the current invocation, as the real meter does.
    """

    def __init__(self, db: Any) -> None:
        self.db = db
        self.requests: list[DecisionRequest] = []
        self.invocations: list[str | None] = []
        self.probability = 0.9
        self.error: Exception | None = None
        self.cost_pusd = 1_000_000

    async def decide(
        self, request: DecisionRequest, *, agent_id: str | None
    ) -> DecisionResult:
        self.requests.append(request)
        invocation = current_invocation.get()
        self.invocations.append(invocation)
        if self.error is not None:
            raise self.error
        now = datetime.now(UTC).isoformat()
        cost = self.cost_pusd
        await self.db.run(
            lambda c: c.execute(
                "INSERT INTO provider_calls (started_at, finished_at, budget_day,"
                " agent_id, provider, call_type, model, status, cost_pusd,"
                " cost_source, invocation_id) VALUES (?, ?, ?, ?, 'openai',"
                " 'decision', 'gpt-6-luna', 'ok', ?, 'provider', ?)",
                (now, now, now[:10], agent_id, cost, invocation),
            )
        )
        name = request.questions[0].name
        return DecisionResult(
            "gpt-6-luna",
            {name: YesNoAnswer(name, self.probability)},
            TokenUsage(),
        )


class FakeMeter:
    def __init__(self) -> None:
        self.remaining = Decimal(1)

    async def budget_state(self, agent_id: str | None) -> BudgetState:
        return BudgetState("2026-11-15", Decimal(0), Decimal(0), self.remaining)


class FakeProviders:
    def __init__(self, db: Any) -> None:
        self.fake_decisions = FakeDecisions(db)
        self.fake_meter = FakeMeter()

    @property
    def decisions(self) -> FakeDecisions:
        return self.fake_decisions

    @property
    def meter(self) -> FakeMeter:
        return self.fake_meter


# ---------------------------------------------------------------- fixture


@dataclass
class Env:
    h: Harness
    tools: ToolsComponent
    rules: RulesComponent
    backtest: BacktestComponent
    router: RouterComponent
    turns: FakeTurns
    providers: FakeProviders
    results: list[InvocationResult] = field(default_factory=list)

    @property
    def decisions(self) -> FakeDecisions:
        return self.providers.fake_decisions

    async def agent(self, **changes: Any) -> Agent:
        changes.setdefault("tools__self", True)
        changes.setdefault("inputs__entities", [TEMP])
        changes.setdefault("outputs", [FLOW])
        return await self.h.agent(**changes)

    async def call(self, agent: Agent, tool: str, args: Mapping[str, Any]) -> Any:
        ctx = ToolContext(agent, invocation_id="inv-setup")
        result = await self.tools.executor().execute(ctx, tool, args)
        assert result.ok, (result.error_code, result.error)
        return result.data

    async def rule(self, agent: Agent, *, activate: bool = False, **parts: Any) -> str:
        spec: dict[str, Any] = {
            "name": "probe",
            "match": "{{ true }}",
            "action": {"type": "template", "template": "on"},
        }
        spec.update(parts)
        created = await self.call(agent, "self.create", {"kind": "rule", "spec": spec})
        if activate:
            await self.call(
                agent,
                "self.set_enabled",
                {"kind": "rule", "item_id": created["id"], "enabled": True},
            )
        return str(created["id"])

    async def temp(self, value: str, unit: str = "°C") -> None:
        await self.h.ha.set_state(TEMP, value, {"unit_of_measurement": unit})
        await wait_until(
            lambda: (s := self.h.states.get(TEMP)) is not None and s.state == value
        )

    async def invoke(
        self, agent: Agent, data: Mapping[str, Any] | None = None
    ) -> InvocationResult:
        result = await self.router.invoke(
            InvocationRequest(agent.id, "invoke", data=data or {})
        )
        self.results.append(result)
        return result

    async def age(self, invocation_id: str, days: float) -> None:
        """Move an invocation and its record ``days`` into the past."""
        at = (datetime.now(UTC) - timedelta(days=days)).isoformat()

        def run(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE invocations SET created_at = ?, started_at = ? WHERE id = ?",
                (at, at, invocation_id),
            )
            conn.execute(
                "UPDATE invocation_records SET created_at = ? WHERE invocation_id = ?",
                (at, invocation_id),
            )

        await self.h.db.run(run)

    async def record(self, invocation_id: str) -> dict[str, Any] | None:
        def run(conn: sqlite3.Connection) -> dict[str, Any] | None:
            row = conn.execute(
                "SELECT * FROM invocation_records WHERE invocation_id = ?",
                (invocation_id,),
            ).fetchone()
            if row is None:
                return None
            data = dict(row)
            for key in ("inputs", "states", "context", "decisions"):
                data[key] = json.loads(data[key])
            return data

        return await self.h.db.run(run)

    async def rule_stats(self, agent: Agent, rule_id: str) -> dict[str, Any]:
        return await self.rules.store.rule_stats(agent.id, rule_id)


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    async with harness(tmp_path) as h:
        h.ha.time_zone = "Europe/London"
        config = Config(ha_url=h.ha.url, ha_token=h.ha.token, data_dir=tmp_path)
        ctx = AppContext(config, h.db, h.client, h.states, "test-installation")
        tools = ToolsComponent()
        providers = FakeProviders(h.db)
        backtest = BacktestComponent(tools, providers)
        rules = RulesComponent(tools, backtest.decision_source())
        backtest.rules = rules
        turns = FakeTurns(tools)
        backtest.turns = turns
        tools.agent_state = rules.agent_state
        router = RouterComponent(turns, tools=tools, rules=backtest)
        await tools.start(ctx)
        await rules.start(ctx)
        await backtest.start(ctx)
        await router.start(ctx)
        await wait_until(
            lambda: tools.registry is not None and not tools.registry.stale
        )
        try:
            yield Env(h, tools, rules, backtest, router, turns, providers)
        finally:
            await router.stop()
            await backtest.stop()
            await rules.stop()
            await tools.stop()


def _const(value: Any) -> Callable[[TurnInput], Any]:
    def answer(_turn: TurnInput) -> Any:
        return value

    return answer


async def _history(
    env: Env, agent: Agent, readings: list[tuple[str, Any]]
) -> list[str]:
    """Agent-turn invocations: one for each (temperature, agent result)."""
    ids = []
    for value, answer in readings:
        await env.temp(value)
        env.turns.answer = _const(answer)
        result = await env.invoke(agent)
        assert (result.status, result.path) == ("ok", "agent_turn")
        ids.append(result.invocation_id)
    return ids


AGREE_WITHIN_ONE = "{{ (rule_result | float - agent_result | float) | abs <= 1 }}"


# --------------------------------------------- AC4: inputs and context kept


async def test_each_invocation_keeps_its_inputs_and_full_states(env: Env) -> None:
    agent = await env.agent()
    await env.temp("18.5", "°C")
    result = await env.invoke(agent, {"room": "lounge"})
    record = await env.record(result.invocation_id)
    assert record is not None
    assert record["inputs"] == {TEMP: "18.5"}
    assert record["states"][TEMP]["state"] == "18.5"
    assert record["states"][TEMP]["attributes"] == {"unit_of_measurement": "°C"}
    assert record["source_key"] == "invoke"


async def test_records_keep_the_context_that_a_rule_collects(env: Env) -> None:
    agent = await env.agent(tools__ha={"level": "read", "scope": {"entities": [TEMP]}})
    item = {"type": "history", "name": "temps", "entity_ids": [TEMP], "hours": 3}
    await env.rule(
        agent,
        activate=True,
        context=[item],
        action={"type": "template", "template": "decline"},
    )
    result = await env.invoke(agent)
    assert result.path == "agent_turn"
    record = await env.record(result.invocation_id)
    assert record is not None
    [(key, value)] = record["context"].items()
    assert json.loads(key)["entity_ids"] == [TEMP]
    assert value == {TEMP: [{"mean": 1.0}]}


async def test_retention_deletes_only_old_records(env: Env) -> None:
    agent = await env.agent()
    old, new = await _history(env, agent, [("18", 18), ("19", 19)])
    await env.age(old, 31)
    await env.age(new, 29)
    assert env.backtest.config.keep_days == 30
    deleted, _cases = await env.backtest.purge()
    assert deleted == 1
    assert await env.record(old) is None
    assert await env.record(new) is not None
    # The invocation history itself stays.
    assert await env.router.get(old) is not None


def test_retention_never_drops_below_the_backtest_window() -> None:
    from hagent_app.backtest import BacktestConfig  # noqa: PLC0415

    assert BacktestConfig(retention_days=3, max_days=14).keep_days == 14


# ------------------------------------------------- AC1: replay, no writes


async def test_a_backtest_replays_past_invocations_without_writes(env: Env) -> None:
    agent = await env.agent()
    await _history(env, agent, [("18", 40), ("21", 30), ("15", 60)])
    rule_id = await env.rule(
        agent,
        action={
            "type": "template",
            "template": "{{ 76 - 2 * (states('sensor.lounge_temp') | float) }}",
        },
        writes=[
            {
                "service": "input_number.set_value",
                "entity_id": FLOW,
                "data": {"value": "{{ result }}"},
            }
        ],
        agreement=AGREE_WITHIN_ONE,
    )
    env.h.ha.commands.clear()
    result = await env.backtest.backtester.run(agent, rule_id)
    assert result["status"] == "ok"
    # 76 - 2*15 = 46 (agent 60), 76 - 2*21 = 34 (30), 76 - 2*18 = 40 (40).
    outcomes = [
        (c["rule_result"], c["agent_result"], c["outcome"]) for c in result["cases"]
    ]
    assert outcomes == [
        (46.0, 60, "disagreed"),
        (34.0, 30, "disagreed"),
        (40.0, 40, "agreed"),
    ]
    assert (result["compared"], result["agreed"]) == (3, 1)
    assert result["agreement_rate"] == pytest.approx(1 / 3)
    assert result["cost_usd"] == 0
    # The writes are recorded in the cases, never sent.
    assert result["cases"][2]["writes"] == [
        {
            "service": "input_number.set_value",
            "entity_id": FLOW,
            "data": {"value": 40.0},
        }
    ]
    assert env.h.ha.commands_of("call_service") == []
    # The rule statistics hold the backtest; the live counters stay at 0.
    stats = await env.rule_stats(agent, rule_id)
    [version] = stats["versions"]
    assert version["matched"] == 0
    assert version["answered"] == 0
    assert version["backtest"]["runs"] == 1
    assert version["backtest"]["agreement_rate"] == pytest.approx(1 / 3)
    assert version["backtest"]["cost_usd"] == 0
    # A backtest writes no audit row for the writes.
    audit = await env.h.db.run(
        lambda c: c.execute(
            "SELECT COUNT(*) FROM tool_audit WHERE tool = 'ha.call_service'"
        ).fetchone()[0]
    )
    assert audit == 0


async def test_the_default_window_is_seven_days(env: Env) -> None:
    agent = await env.agent()
    old, recent = await _history(env, agent, [("18", "on"), ("19", "on")])
    await env.age(old, 8)
    rule_id = await env.rule(agent)
    result = await env.backtest.backtester.run(agent, rule_id)
    assert [c["invocation_id"] for c in result["cases"]] == [recent]
    wider = await env.backtest.backtester.run(agent, rule_id, BacktestOptions(days=10))
    assert [c["invocation_id"] for c in wider["cases"]] == [recent, old]


async def test_now_is_the_time_of_the_past_invocation(env: Env) -> None:
    agent = await env.agent()
    [past] = await _history(env, agent, [("18", "x")])
    await env.age(past, 2)
    rule_id = await env.rule(
        agent,
        action={
            "type": "template",
            "template": "{{ now().isoformat() }}|{{ now().tzinfo }}",
        },
    )
    result = await env.backtest.backtester.run(agent, rule_id)
    [case] = result["cases"]
    text, zone = case["rule_result"].split("|")
    at = datetime.fromisoformat(text)
    assert abs(at - datetime.fromisoformat(case["invocation_at"])) < timedelta(
        seconds=1
    )
    assert zone == "Europe/London"


async def test_utcnow_is_the_time_of_the_past_invocation(env: Env) -> None:
    agent = await env.agent()
    [past] = await _history(env, agent, [("18", "x")])
    await env.age(past, 2)
    rule_id = await env.rule(
        agent, action={"type": "template", "template": "{{ utcnow().isoformat() }}"}
    )
    result = await env.backtest.backtester.run(agent, rule_id)
    [case] = result["cases"]
    at = datetime.fromisoformat(case["rule_result"])
    assert abs(at - datetime.fromisoformat(case["invocation_at"])) < timedelta(
        seconds=1
    )


# The task-13 security review fixes must hold in the dry-run paths too: each
# backtest and trial render runs in a worker process with the same limits.
STUCK_REGEX = "{{ ('" + "a" * 30 + "!') is match('(a+)+$') }}"
NAMESPACE_BOMB = (
    "{% set s = 'a' * 250000 %}{% set ns = namespace(l=[s] * 1000) %}{{ ns }}"
)


async def test_backtest_renders_have_the_worker_limits(env: Env) -> None:
    agent = await env.agent()
    await _history(env, agent, [("18", "x"), ("19", "y")])
    pool = shared_pool()
    killed = pool.killed
    stuck = await env.rule(agent, action={"type": "template", "template": STUCK_REGEX})
    started = time.monotonic()
    result = await env.backtest.backtester.run(agent, stuck)
    # Two cases, each killed after the time limit (1 s) plus the grace.
    assert time.monotonic() - started < 8
    assert [c["outcome"] for c in result["cases"]] == ["error", "error"]
    assert all("took longer" in c["detail"] for c in result["cases"])
    assert pool.killed == killed + 2
    bomb = await env.rule(
        agent, action={"type": "template", "template": NAMESPACE_BOMB}
    )
    result = await env.backtest.backtester.run(agent, bomb)
    assert all("larger than" in c["detail"] for c in result["cases"])
    # The agreement template runs in a worker too.
    star = await env.rule(agent, agreement="{{ '%*d' % (500000000, 1) }}")
    result = await env.backtest.backtester.run(agent, star)
    assert all("of *" in c["detail"] for c in result["cases"])


async def test_trial_renders_have_the_worker_limits(env: Env) -> None:
    agent = await env.agent()
    env.backtest.sample = lambda: 0.0
    await env.rule(
        agent,
        activate=True,
        action={"type": "template", "template": "40"},
        trial={"enabled": True, "rate": 0.5},
        agreement=NAMESPACE_BOMB,
    )
    env.turns.answer = lambda _t: 41
    result = await env.invoke(agent)
    assert (result.path, result.result) == ("rule", 40)
    await wait_until(lambda: not env.backtest.trials.tasks)
    [trial] = await env.h.db.run(
        lambda c: [dict(r) for r in c.execute("SELECT * FROM rule_trials")]
    )
    assert trial["status"] == "error"
    assert "larger than" in trial["detail"]


async def test_rule_answers_and_other_sources_are_not_candidates(env: Env) -> None:
    agent = await env.agent()
    await _history(env, agent, [("18", "turn")])
    live = await env.rule(agent, activate=True)
    answered = await env.invoke(agent)
    assert answered.path == "rule"
    await env.router.invoke(InvocationRequest(agent.id, "assist", conversation_id="c1"))
    await env.router.test(agent.id)
    draft = await env.rule(agent)
    result = await env.backtest.backtester.run(agent, draft)
    assert result["candidates"] == 1
    assert live != draft


async def test_a_match_that_fails_is_counted_as_no_match(env: Env) -> None:
    agent = await env.agent()
    await _history(env, agent, [("18", "on"), ("25", "on")])
    rule_id = await env.rule(
        agent, match="{{ states('sensor.lounge_temp') | float < 20 }}"
    )
    result = await env.backtest.backtester.run(agent, rule_id)
    assert [c["outcome"] for c in result["cases"]] == ["no_match", "agreed"]
    assert (result["matched"], result["no_match"]) == (1, 1)


async def test_without_agreement_template_equal_results_agree(env: Env) -> None:
    agent = await env.agent()
    await _history(env, agent, [("18", {"flow": 40}), ("19", {"flow": 41})])
    rule_id = await env.rule(
        agent, action={"type": "template", "template": '{"flow": 41}'}
    )
    result = await env.backtest.backtester.run(agent, rule_id)
    assert [c["outcome"] for c in result["cases"]] == ["agreed", "disagreed"]


async def test_backtest_errors(env: Env) -> None:
    agent = await env.agent()
    other = await env.agent(name="Other")
    rule_id = await env.rule(agent)
    with pytest.raises(BacktestError) as found:
        await env.backtest.backtester.run(other, rule_id)
    assert found.value.code == "rule_not_found"
    with pytest.raises(BacktestError) as found:
        await env.backtest.backtester.run(agent, rule_id, BacktestOptions(version=9))
    assert found.value.code == "version_not_found"


async def test_a_backtest_tests_a_draft_version(env: Env) -> None:
    agent = await env.agent()
    await _history(env, agent, [("18", "on")])
    rule_id = await env.rule(agent, activate=True)
    await env.call(
        agent,
        "self.update",
        {
            "kind": "rule",
            "item_id": rule_id,
            "spec": {
                "name": "probe",
                "match": "{{ true }}",
                "action": {"type": "template", "template": "off"},
            },
        },
    )
    v2 = await env.backtest.backtester.run(agent, rule_id)
    v1 = await env.backtest.backtester.run(agent, rule_id, BacktestOptions(version=1))
    assert (v2["version"], v2["agreed"]) == (2, 0)
    assert (v1["version"], v1["agreed"]) == (1, 1)
    stats = await env.rule_stats(agent, rule_id)
    assert [v["backtest"]["agreement_rate"] for v in stats["versions"]] == [1.0, 0.0]


# ------------------------------------------------------ context in a replay


async def test_history_context_ends_at_the_invocation_time(env: Env) -> None:
    agent = await env.agent(tools__ha={"level": "read", "scope": {"entities": [TEMP]}})
    [past] = await _history(env, agent, [("18", "on")])
    await env.age(past, 1)
    rule_id = await env.rule(
        agent,
        context=[{"type": "history", "name": "h", "entity_ids": [TEMP], "hours": 6}],
        action={"type": "template", "template": "on"},
    )
    env.h.ha.commands.clear()
    result = await env.backtest.backtester.run(agent, rule_id)
    assert result["agreed"] == 1
    [command] = env.h.ha.commands_of("recorder/statistics_during_period")
    case_at = datetime.fromisoformat(result["cases"][0]["invocation_at"])
    end = datetime.fromisoformat(command["end_time"])
    start = datetime.fromisoformat(command["start_time"])
    assert abs(end - case_at) < timedelta(seconds=1)
    assert end - start == timedelta(hours=6)


async def test_an_out_of_scope_history_item_never_reaches_ha(env: Env) -> None:
    agent = await env.agent(
        tools__ha={"level": "read", "scope": {"entities": [TEMP, "lock.front"]}}
    )
    await _history(env, agent, [("18", "on")])
    rule_id = await env.rule(
        agent,
        context=[{"type": "history", "name": "h", "entity_ids": ["lock.front"]}],
    )
    # The scope shrinks after the rule was written.
    data = agent.definition.to_data()
    data["tools"]["ha"]["scope"]["entities"] = [TEMP]
    from hagent_app.agents.definition import AgentDefinition  # noqa: PLC0415

    agent = await env.h.agents.update(
        agent.id, AgentDefinition.from_json(json.dumps(data))
    )
    env.h.ha.commands.clear()
    result = await env.backtest.backtester.run(agent, rule_id)
    [case] = result["cases"]
    assert case["outcome"] == "error"
    assert "out_of_scope" in case["detail"]
    assert env.h.ha.commands_of("recorder/statistics_during_period") == []


async def test_action_response_context_comes_from_the_turn(env: Env) -> None:
    agent = await env.agent(
        tools__ha={"level": "control", "scope": {"entities": ["weather.home"]}}
    )
    first, second = await _history(env, agent, [("18", "rain"), ("19", "sun")])
    history = TurnHistory(env.h.db)
    turn_id = "f" * 32
    await history.begin(
        turn_id=turn_id,
        agent_id=agent.id,
        agent_version=agent.version,
        invocation_id=first,
        source="invoke",
        source_key="invoke",
        conversation_id=None,
        dry_run=False,
        input_data={},
        instructions_sha256="0" * 64,
        prompt="p",
    )
    arguments = {
        "domain": "weather",
        "service": "get_forecasts",
        "target": {"entity_id": "weather.home"},
        "data": {"type": "hourly"},
        "return_response": True,
    }
    response = {"weather.home": {"forecast": [{"condition": "rainy"}]}}
    await history.add_tool_call(
        turn_id,
        tool_call_id="c1",
        tool="ha.call_service",
        arguments=arguments,
        ok=True,
        error_code=None,
        result_text=json.dumps({"ok": True, "data": {"response": response}}),
        cut=False,
        audit_id=None,
    )
    await env.h.db.run(
        lambda c: c.execute(
            "UPDATE invocations SET turn_id = ? WHERE id = ?", (turn_id, first)
        )
    )
    rule_id = await env.rule(
        agent,
        context=[
            {
                "type": "action_response",
                "name": "fc",
                "service": "weather.get_forecasts",
                "target": {"entity_id": "weather.home"},
                "data": {"type": "hourly"},
            }
        ],
        action={
            "type": "template",
            "template": (
                "{{ 'rain' if context.fc['weather.home'].forecast[0].condition"
                " == 'rainy' else 'sun' }}"
            ),
        },
    )
    env.h.ha.commands.clear()
    result = await env.backtest.backtester.run(agent, rule_id)
    by_id = {c["invocation_id"]: c for c in result["cases"]}
    assert by_id[first]["outcome"] == "agreed"
    assert by_id[second]["outcome"] == "skipped"
    assert "no recorded response" in by_id[second]["detail"]
    # Skipped cases do not count in the agreement rate.
    assert (result["compared"], result["skipped"]) == (1, 1)
    assert result["agreement_rate"] == 1.0
    # The data action never ran again.
    assert env.h.ha.commands_of("call_service") == []


async def test_invocation_history_context_ends_before_the_case(env: Env) -> None:
    agent = await env.agent()
    ids = await _history(env, agent, [("18", 1), ("19", 2), ("20", 3)])
    for days, invocation_id in zip((3, 2, 1), ids, strict=True):
        await env.age(invocation_id, days)
    rule_id = await env.rule(
        agent,
        context=[{"type": "invocation_history", "name": "past", "limit": 10}],
        action={"type": "template", "template": "{{ context.past | count + 1 }}"},
    )
    result = await env.backtest.backtester.run(agent, rule_id)
    # Each case sees only the invocations before it.
    assert [c["rule_result"] for c in result["cases"]] == [3, 2, 1]
    assert result["agreed"] == 3


# ------------------------------------------------------ AC2: decision rules


async def test_decision_backtests_can_be_limited(env: Env) -> None:
    agent = await env.agent(models__decision=DECISION)
    await _history(env, agent, [(str(t), "cold") for t in range(15, 20)])
    rule_id = await env.rule(
        agent,
        action=YES_NO,
        result="{{ 'cold' if answers.cold.probability > 0.5 else 'warm' }}",
    )
    result = await env.backtest.backtester.run(agent, rule_id, BacktestOptions(limit=2))
    assert len(env.decisions.requests) == 2
    assert (result["matched"], result["live_decisions"]) == (2, 2)
    assert [c["decision"] for c in result["cases"]] == ["live", "live"]
    # The cost carries the backtest ID, so it is exact.
    assert env.decisions.invocations == [result["id"], result["id"]]
    assert result["cost_usd"] == pytest.approx(2e-6)
    stats = await env.rule_stats(agent, rule_id)
    assert stats["versions"][0]["backtest"]["cost_usd"] == pytest.approx(2e-6)
    assert stats["versions"][0]["cost_usd"] == 0


async def test_decision_backtests_have_a_default_limit(env: Env) -> None:
    agent = await env.agent(models__decision=DECISION)
    await _history(env, agent, [(str(t), "cold") for t in range(15, 20)])
    rule_id = await env.rule(agent, action=YES_NO)
    env.backtest.backtester.config = type(env.backtest.config)(decision_cases=3)
    result = await env.backtest.backtester.run(agent, rule_id)
    assert result["max_cases"] == 3
    assert len(env.decisions.requests) == 3


async def test_recorded_decisions_are_replayed(env: Env) -> None:
    agent = await env.agent(models__decision=DECISION)
    # A live decision rule that declines: the agent turn answers, and the
    # record keeps the decision answer.
    live = await env.rule(agent, activate=True, action=YES_NO, result="decline")
    await env.temp("17")
    env.turns.answer = lambda _t: "cold"
    answered = await env.invoke(agent)
    assert answered.path == "agent_turn"
    record = await env.record(answered.invocation_id)
    assert record is not None
    assert len(record["decisions"]) == 1
    await env.call(
        agent, "self.set_enabled", {"kind": "rule", "item_id": live, "enabled": False}
    )
    env.decisions.requests.clear()
    draft = await env.rule(
        agent,
        action=YES_NO,
        result="{{ 'cold' if answers.cold.probability > 0.5 else 'warm' }}",
    )
    result = await env.backtest.backtester.run(agent, draft)
    [case] = result["cases"]
    assert (case["decision"], case["outcome"]) == ("replayed", "agreed")
    assert env.decisions.requests == []
    assert (result["replayed_decisions"], result["cost_usd"]) == (1, 0)


async def test_live_false_skips_invocations_without_recorded_answers(
    env: Env,
) -> None:
    agent = await env.agent(models__decision=DECISION)
    await _history(env, agent, [("17", "cold")])
    rule_id = await env.rule(agent, action=YES_NO)
    result = await env.backtest.backtester.run(
        agent, rule_id, BacktestOptions(live=False)
    )
    assert [c["outcome"] for c in result["cases"]] == ["skipped"]
    assert env.decisions.requests == []


async def test_a_spent_budget_stops_the_backtest(env: Env) -> None:
    agent = await env.agent(models__decision=DECISION)
    await _history(env, agent, [("17", "cold"), ("18", "cold")])
    rule_id = await env.rule(agent, action=YES_NO)
    env.decisions.error = BudgetExceededError("daily budget is spent", "agent")
    result = await env.backtest.backtester.run(agent, rule_id)
    assert result["status"] == "budget"
    assert result["cases"] == []
    assert len(env.decisions.requests) == 1


# --------------------------------------------------------------- AC3: trials


async def test_a_trial_turn_writes_nothing_and_compares(env: Env) -> None:
    agent = await env.agent()
    env.backtest.sample = lambda: 0.0
    rule_id = await env.rule(
        agent,
        activate=True,
        action={"type": "template", "template": "40"},
        trial={"enabled": True, "rate": 0.5},
        agreement=AGREE_WITHIN_ONE,
    )
    env.turns.write = True
    env.turns.answer = lambda _t: 41
    env.h.ha.commands.clear()
    result = await env.invoke(agent)
    assert (result.path, result.result) == ("rule", 40)
    await wait_until(lambda: not env.backtest.trials.tasks)
    [turn] = env.turns.turns
    assert turn.dry_run is True
    assert turn.invocation_id != result.invocation_id
    [write] = env.turns.write_results
    assert write.data["dry_run"] is True
    assert env.h.ha.commands_of("call_service") == []
    [trial] = await env.h.db.run(
        lambda c: [dict(r) for r in c.execute("SELECT * FROM rule_trials")]
    )
    assert (trial["status"], trial["invocation_id"]) == ("agreed", result.invocation_id)
    stats = await env.rule_stats(agent, rule_id)
    assert stats["versions"][0]["trial"]["runs"] == 1
    assert stats["versions"][0]["trial"]["agreement_rate"] == 1.0
    # The live invocation keeps its own path and cost.
    row = await env.router.get(result.invocation_id)
    assert row is not None
    assert row["path"] == "rule"


async def test_trials_follow_the_sample_rate(env: Env) -> None:
    agent = await env.agent()
    samples = iter([0.6, 0.1])
    env.backtest.sample = lambda: next(samples)
    await env.rule(agent, activate=True, trial={"enabled": True, "rate": 0.5})
    await env.invoke(agent)
    await env.invoke(agent)
    await wait_until(lambda: not env.backtest.trials.tasks)
    assert len(env.turns.turns) == 1


async def test_a_spent_budget_stops_trials(env: Env) -> None:
    agent = await env.agent()
    env.backtest.sample = lambda: 0.0
    rule_id = await env.rule(agent, activate=True, trial={"enabled": True})
    env.providers.fake_meter.remaining = Decimal(0)
    await env.invoke(agent)
    await wait_until(lambda: not env.backtest.trials.tasks)
    assert env.turns.turns == []
    stats = await env.rule_stats(agent, rule_id)
    assert stats["versions"][0]["trial"]["budget"] == 1


async def test_a_trial_turn_that_hits_the_budget_records_it(env: Env) -> None:
    agent = await env.agent()
    env.backtest.sample = lambda: 0.0
    await env.rule(agent, activate=True, trial={"enabled": True})

    async def spent(
        agent: Agent, turn: TurnInput, *, states: StateView | None = None
    ) -> TurnResult:
        return TurnResult("t1", "failed", stop_reason="budget", error="spent")

    env.turns.run_turn = spent  # type: ignore[method-assign]
    await env.invoke(agent)
    await wait_until(lambda: not env.backtest.trials.tasks)
    statuses = await env.h.db.run(
        lambda c: [r[0] for r in c.execute("SELECT status FROM rule_trials")]
    )
    assert statuses == ["budget"]


async def test_one_trial_at_a_time_for_each_rule(env: Env) -> None:
    agent = await env.agent()
    env.backtest.sample = lambda: 0.0
    await env.rule(agent, activate=True, trial={"enabled": True})
    env.turns.gate = asyncio.Event()
    await env.invoke(agent)
    await wait_until(lambda: len(env.turns.turns) == 1)
    await env.invoke(agent)
    assert len(env.backtest.trials.tasks) == 1
    env.turns.gate.set()
    await wait_until(lambda: not env.backtest.trials.tasks)
    assert len(env.turns.turns) == 1


async def test_stop_cancels_running_trials(env: Env) -> None:
    agent = await env.agent()
    env.backtest.sample = lambda: 0.0
    await env.rule(agent, activate=True, trial={"enabled": True})
    env.turns.gate = asyncio.Event()
    await env.invoke(agent)
    await wait_until(lambda: len(env.turns.turns) == 1)
    await env.backtest.trials.stop()
    statuses = await env.h.db.run(
        lambda c: [r[0] for r in c.execute("SELECT status FROM rule_trials")]
    )
    assert statuses == ["cancelled"]


async def test_a_backtest_compares_with_trial_results(env: Env) -> None:
    agent = await env.agent()
    env.backtest.sample = lambda: 0.0
    live = await env.rule(
        agent,
        activate=True,
        action={"type": "template", "template": "40"},
        trial={"enabled": True},
    )
    env.turns.answer = lambda _t: 40
    answered = await env.invoke(agent)
    await wait_until(lambda: not env.backtest.trials.tasks)
    draft = await env.rule(agent, action={"type": "template", "template": "40"})
    result = await env.backtest.backtester.run(agent, draft)
    [case] = result["cases"]
    assert (case["invocation_id"], case["outcome"]) == (
        answered.invocation_id,
        "agreed",
    )
    assert live != draft


# ------------------------------------------------- parity with the live stage


async def test_the_dry_run_matches_the_live_rule(env: Env) -> None:
    agent = await env.agent()
    template = (
        "{{ states('sensor.lounge_temp') | float * 2 }}"
        "|{{ state_attr('sensor.lounge_temp', 'unit_of_measurement') }}"
        "|{{ data.room }}|{{ source }}|{{ inputs['sensor.lounge_temp'] }}"
    )
    rule_id = await env.rule(
        agent, activate=True, action={"type": "template", "template": template}
    )
    await env.temp("18.5", "°F")
    live = await env.invoke(agent, {"room": "lounge"})
    assert live.path == "rule"
    # Turn the rule into history with a known agent result.
    await env.h.db.run(
        lambda c: c.execute(
            "UPDATE invocations SET path = 'agent_turn', result = ? WHERE id = ?",
            (json.dumps(live.result), live.invocation_id),
        )
    )
    # Today's state differs; the replay uses the recorded one.
    await env.temp("25", "°C")
    result = await env.backtest.backtester.run(agent, rule_id)
    [case] = result["cases"]
    assert case["rule_result"] == live.result == "37.0|°F|lounge|invoke|18.5"
    assert case["outcome"] == "agreed"


async def test_history_from_before_records_uses_the_turn_input(env: Env) -> None:
    agent = await env.agent()
    [past] = await _history(env, agent, [("18", 36.0)])
    await env.h.db.run(
        lambda c: c.execute(
            "DELETE FROM invocation_records WHERE invocation_id = ?", (past,)
        )
    )
    history = TurnHistory(env.h.db)
    turn_id = "e" * 32
    await history.begin(
        turn_id=turn_id,
        agent_id=agent.id,
        agent_version=agent.version,
        invocation_id=past,
        source="invoke",
        source_key="invoke",
        conversation_id=None,
        dry_run=False,
        input_data={"inputs": {TEMP: "18"}, "data": {}},
        instructions_sha256="0" * 64,
        prompt="p",
    )
    await env.h.db.run(
        lambda c: c.execute(
            "UPDATE invocations SET turn_id = ? WHERE id = ?", (turn_id, past)
        )
    )
    await env.temp("30")
    rule_id = await env.rule(
        agent,
        action={
            "type": "template",
            "template": "{{ inputs['sensor.lounge_temp'] | float * 2 }}",
        },
    )
    result = await env.backtest.backtester.run(agent, rule_id)
    [case] = result["cases"]
    assert case["rule_result"] == 36.0
    assert case["outcome"] == "agreed"


# ------------------------------------------------------------ the self tool


async def test_the_self_tool_runs_a_backtest(env: Env) -> None:
    agent = await env.agent()
    await _history(env, agent, [("18", "on"), ("19", "off")])
    rule_id = await env.rule(agent)
    data = await env.call(agent, "self.backtest_rule", {"rule_id": rule_id})
    assert (data["compared"], data["agreed"]) == (2, 1)
    assert "cases" not in data
    assert [c["outcome"] for c in data["notable_cases"]] == ["disagreed"]


async def test_a_dry_run_does_not_run_the_backtest(env: Env) -> None:
    agent = await env.agent()
    rule_id = await env.rule(agent)
    ctx = ToolContext(agent, invocation_id="inv-test", dry_run=True)
    result = await env.tools.executor().execute(
        ctx, "self.backtest_rule", {"rule_id": rule_id}
    )
    assert result.ok
    assert result.data["dry_run"] is True
    count = await env.h.db.run(
        lambda c: c.execute("SELECT COUNT(*) FROM rule_backtests").fetchone()[0]
    )
    assert count == 0


async def test_close_running_marks_old_runs(env: Env) -> None:
    from hagent_app.backtest import store  # noqa: PLC0415

    agent = await env.agent()
    rule_id = await env.rule(agent)
    await env.h.db.run(
        lambda c: store.insert_backtest(
            c,
            "a" * 32,
            agent_id=agent.id,
            rule_id=rule_id,
            version=1,
            agent_version=1,
            since="x",
            until="y",
            max_cases=1,
        )
    )
    await env.h.db.run(store.close_running)
    found = await env.h.db.run(lambda c: store.get_backtest(c, agent.id, "a" * 32))
    assert found is not None
    assert found["status"] == "cancelled"


async def test_purge_deletes_old_backtest_cases(env: Env) -> None:
    agent = await env.agent()
    await _history(env, agent, [("18", "on")])
    rule_id = await env.rule(agent)
    result = await env.backtest.backtester.run(agent, rule_id)
    old = (datetime.now(UTC) - timedelta(days=40)).isoformat()
    await env.h.db.run(
        lambda c: c.execute(
            "UPDATE rule_backtests SET started_at = ? WHERE id = ?",
            (old, result["id"]),
        )
    )
    deleted = await env.h.db.run(
        lambda c: backtest_records.purge(c, 30, datetime.now(UTC))
    )
    assert deleted[1] == 1


# --------------------------------------------------------------- the API


@pytest.fixture
async def api(env: Env) -> AsyncIterator[TestClient[Any, Any]]:
    http = HttpServer(host="127.0.0.1", port=0)
    http.add_routes(env.backtest.routes())
    async with TestClient(TestServer(http.make_app())) as client:
        yield client


async def _json(resp: Any) -> tuple[int, Any]:
    return resp.status, await resp.json()


async def test_the_rule_routes(env: Env, api: TestClient[Any, Any]) -> None:
    agent = await env.agent()
    await _history(env, agent, [("18", "on"), ("19", "off")])
    rule_id = await env.rule(agent, activate=True)
    await env.call(
        agent,
        "self.update",
        {
            "kind": "rule",
            "item_id": rule_id,
            "spec": {
                "name": "v2",
                "match": "{{ true }}",
                "action": {"type": "template", "template": "off"},
            },
        },
    )
    base = f"/api/agents/{agent.id}/rules"

    status, body = await _json(await api.get(base))
    assert status == 200
    assert [r["id"] for r in body["rules"]] == [rule_id]

    status, body = await _json(await api.get(f"{base}/{rule_id}"))
    assert status == 200
    assert (body["current_version"], body["spec"]["name"]) == (2, "v2")
    assert [v["version"] for v in body["versions"]] == [1, 2]
    assert body["versions"][0]["backtest"]["runs"] == 0

    status, body = await _json(await api.get(f"{base}/{rule_id}/versions/1"))
    assert (status, body["spec"]["name"]) == (200, "probe")
    status, body = await _json(await api.get(f"{base}/{rule_id}/versions/7"))
    assert (status, body["error"]["code"]) == (404, "version_not_found")

    status, body = await _json(
        await api.post(f"{base}/{rule_id}/backtests", json={"version": 1, "days": 3})
    )
    assert status == 200
    assert (body["version"], body["agreed"], body["compared"]) == (1, 1, 2)
    assert len(body["cases"]) == 2
    backtest_id = body["id"]

    status, body = await _json(await api.get(f"{base}/{rule_id}/backtests"))
    assert [b["id"] for b in body["backtests"]] == [backtest_id]
    assert "cases" not in body["backtests"][0]
    status, body = await _json(
        await api.get(f"{base}/{rule_id}/backtests/{backtest_id}")
    )
    assert (status, len(body["cases"])) == (200, 2)

    status, body = await _json(
        await api.post(f"{base}/{rule_id}/rollback", json={"version": 1})
    )
    assert (status, body["version"]) == (200, 1)
    status, body = await _json(
        await api.post(f"{base}/{rule_id}/rollback", json={"version": 9})
    )
    assert (status, body["error"]["code"]) == (404, "version_not_found")

    status, body = await _json(await api.get(f"{base}/{rule_id}/trials"))
    assert (status, body) == (200, {"trials": []})


async def test_the_rule_routes_keep_agents_apart(
    env: Env, api: TestClient[Any, Any]
) -> None:
    owner = await env.agent()
    other = await env.agent(name="Other")
    rule_id = await env.rule(owner)
    base = f"/api/agents/{other.id}/rules/{rule_id}"
    for resp in (
        await api.get(base),
        await api.get(f"{base}/versions/1"),
        await api.post(f"{base}/rollback", json={"version": 1}),
        await api.post(f"{base}/backtests", json={}),
    ):
        status, body = await _json(resp)
        assert (status, body["error"]["code"]) == (404, "rule_not_found")
    status, body = await _json(await api.get(f"/api/agents/{other.id}/rules"))
    assert body == {"rules": []}
    status, body = await _json(await api.get(f"/api/agents/{'0' * 32}/rules"))
    assert (status, body["error"]["code"]) == (404, "agent_not_found")


async def test_the_user_turns_a_rule_off_and_on(
    env: Env, api: TestClient[Any, Any]
) -> None:
    agent = await env.agent()
    rule_id = await env.rule(agent, activate=True)
    await env.temp("18")
    assert (await env.invoke(agent)).path == "rule"
    base = f"/api/agents/{agent.id}/rules/{rule_id}"

    status, body = await _json(await api.post(f"{base}/state", json={"state": "off"}))
    assert (status, body) == (200, {"id": rule_id, "version": 1, "state": "off"})
    assert (await env.rule_stats(agent, rule_id))["state"] == "off"
    # An off rule never answers: the agent turn answers.
    assert (await env.invoke(agent)).path == "agent_turn"

    status, body = await _json(
        await api.post(f"{base}/state", json={"state": "active"})
    )
    assert (status, body["state"]) == (200, "active")
    assert (await env.invoke(agent)).path == "rule"

    for bad in ({}, {"state": "draft"}, {"state": True}):
        status, body = await _json(await api.post(f"{base}/state", json=bad))
        assert (status, body["error"]["code"]) == (400, "invalid_body")


async def test_the_user_cannot_activate_a_draft_rule(
    env: Env, api: TestClient[Any, Any]
) -> None:
    agent = await env.agent()
    rule_id = await env.rule(agent)
    base = f"/api/agents/{agent.id}/rules/{rule_id}"
    status, body = await _json(
        await api.post(f"{base}/state", json={"state": "active"})
    )
    assert (status, body["error"]["code"]) == (409, "rule_is_draft")
    status, body = await _json(await api.post(f"{base}/state", json={"state": "off"}))
    assert (status, body["state"]) == (200, "off")


async def test_the_user_deletes_a_rule(env: Env, api: TestClient[Any, Any]) -> None:
    agent = await env.agent()
    rule_id = await env.rule(agent, activate=True)
    await env.temp("18")
    assert (await env.invoke(agent)).path == "rule"
    base = f"/api/agents/{agent.id}/rules/{rule_id}"

    status, body = await _json(await api.delete(base))
    assert (status, body) == (200, {"deleted": rule_id})
    assert (await env.invoke(agent)).path == "agent_turn"
    status, body = await _json(await api.get(f"/api/agents/{agent.id}/rules"))
    assert body == {"rules": []}
    # The versions and statistics stay for history.
    stats = await env.rule_stats(agent, rule_id)
    assert stats["deleted"] is True
    assert stats["versions"][0]["answered"] == 1
    # A deleted rule cannot change again.
    for resp in (
        await api.delete(base),
        await api.post(f"{base}/state", json={"state": "active"}),
        await api.post(f"{base}/state", json={"state": "off"}),
    ):
        status, body = await _json(resp)
        assert (status, body["error"]["code"]) == (404, "rule_not_found")


async def test_state_and_delete_act_only_on_the_agents_own_rules(
    env: Env, api: TestClient[Any, Any]
) -> None:
    owner = await env.agent()
    other = await env.agent(name="Other")
    rule_id = await env.rule(owner, activate=True)
    base = f"/api/agents/{other.id}/rules/{rule_id}"
    for resp in (
        await api.post(f"{base}/state", json={"state": "off"}),
        await api.delete(base),
    ):
        status, body = await _json(resp)
        assert (status, body["error"]["code"]) == (404, "rule_not_found")
    stats = await env.rule_stats(owner, rule_id)
    assert (stats["state"], stats["deleted"]) == ("active", False)


async def test_backtest_route_checks_the_body(
    env: Env, api: TestClient[Any, Any]
) -> None:
    agent = await env.agent()
    rule_id = await env.rule(agent)
    url = f"/api/agents/{agent.id}/rules/{rule_id}/backtests"
    bodies: list[Any] = [{"days": 0}, {"days": 31}, {"limit": "5"}, {"live": 1}, []]
    for body in bodies:
        status, data = await _json(await api.post(url, json=body))
        assert (status, data["error"]["code"]) == (400, "invalid_body")


async def test_the_template_check_route(env: Env, api: TestClient[Any, Any]) -> None:
    url = "/api/rules/check-template"
    status, body = await _json(await api.post(url, json={"template": "{{ 1 + 1 }}"}))
    assert (status, body) == (200, {"ok": True, "error": None})
    status, body = await _json(
        await api.post(url, json={"template": "{{ area_entities('x') }}"})
    )
    assert status == 200
    assert body["ok"] is False
    assert "area_entities" in body["error"]
    status, body = await _json(await api.post(url, json={"template": ""}))
    assert (status, body["error"]["code"]) == (400, "invalid_body")


async def test_the_rule_routes_answer_503_before_start(tmp_path: Path) -> None:
    async with harness(tmp_path) as h:
        tools = ToolsComponent()
        backtest = BacktestComponent(tools, FakeProviders(h.db))
        http = HttpServer(host="127.0.0.1", port=0)
        http.add_routes(backtest.routes())
        async with TestClient(TestServer(http.make_app())) as client:
            resp = await client.get(f"/api/agents/{'a' * 32}/rules")
            assert resp.status == 503
            assert (await resp.json())["error"]["code"] == "not_ready"


async def test_a_trial_counts_towards_the_budget_of_the_agent(env: Env) -> None:
    agent = await env.agent()
    env.backtest.sample = lambda: 0.0
    rule_id = await env.rule(agent, activate=True, trial={"enabled": True})
    seen: list[str | None] = []

    async def billed(
        agent: Agent, turn: TurnInput, *, states: StateView | None = None
    ) -> TurnResult:
        # The meter writes this row for each chat request of a turn.
        invocation = current_invocation.get()
        seen.append(invocation)
        now = datetime.now(UTC).isoformat()
        await env.h.db.run(
            lambda c: c.execute(
                "INSERT INTO provider_calls (started_at, finished_at, budget_day,"
                " agent_id, provider, call_type, model, status, cost_pusd,"
                " cost_source, invocation_id) VALUES (?, ?, ?, ?, 'openai',"
                " 'chat', 'gpt-5-mini', 'ok', 3000000, 'provider', ?)",
                (now, now, now[:10], agent.id, invocation),
            )
        )
        return TurnResult(f"t-{turn.invocation_id}", "ok", result="on")

    env.turns.run_turn = billed  # type: ignore[method-assign]
    live = await env.invoke(agent)
    await wait_until(lambda: not env.backtest.trials.tasks)
    [trial] = await env.h.db.run(
        lambda c: [dict(r) for r in c.execute("SELECT * FROM rule_trials")]
    )
    # The cost carries the trial ID, not the live invocation.
    assert seen == [trial["id"]]
    assert trial["cost_pusd"] == 3_000_000
    stats = await env.rule_stats(agent, rule_id)
    assert stats["versions"][0]["trial"]["cost_usd"] == pytest.approx(3e-6)
    assert stats["versions"][0]["cost_usd"] == 0
    row = await env.router.get(live.invocation_id)
    assert row is not None
    assert row["cost_pusd"] == 0
    # The budget of the agent sums its provider calls: the trial counts.
    spent = await env.h.db.run(
        lambda c: c.execute(
            "SELECT SUM(cost_pusd) FROM provider_calls WHERE agent_id = ?",
            (agent.id,),
        ).fetchone()[0]
    )
    assert spent == 3_000_000


async def test_a_rendered_write_gets_the_service_call_check(env: Env) -> None:
    agent = await env.agent()
    await _history(env, agent, [("18", "on")])
    # The create check sees placeholder data only. The rendered value is
    # template text, which the service call check denies, as it does for a
    # live write.
    rule_id = await env.rule(
        agent,
        writes=[
            {
                "service": "input_number.set_value",
                "entity_id": FLOW,
                "data": {"value": "{{ '{' ~ '{ 1 }' ~ '}' }}"},
            }
        ],
    )
    env.h.ha.commands.clear()
    result = await env.backtest.backtester.run(agent, rule_id)
    [case] = result["cases"]
    assert case["outcome"] == "error", case
    assert case["detail"].startswith("template:")
    assert case["writes"] == []
    assert env.h.ha.commands_of("call_service") == []
