"""Tests of the router (task-12): modes, Assist lanes, fallback, restart.

The agent turns come from ``FakeTurns``, a scripted turn component. The cost
and retry tests use the real ``TurnRunner`` with a Pydantic AI
``FunctionModel`` behind a real ``Meter``. No test calls a provider or a
live HA instance.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from hagent_app.agents import Agent, AgentDefinition
from hagent_app.app import AppContext
from hagent_app.config import Config
from hagent_app.providers import (
    BudgetExceededError,
    ConfigKeySource,
    Meter,
    MeteredModel,
    PriceTable,
    ProviderHTTPError,
)
from hagent_app.providers import ProvidersConfig as PConfig
from hagent_app.router import (
    InvocationRequest,
    InvocationResult,
    RetryingModel,
    RetryPolicy,
    RouterComponent,
    RouterNotRunningError,
    RuleOutcome,
    RuleRequest,
    retryable,
)
from hagent_app.router import store as router_store
from hagent_app.tools import StateView
from hagent_app.turns import TurnHistory, TurnInput, TurnResult, TurnRunner
from hagent_app.turns.models import TurnRecord
from helpers.tools import Harness, harness
from helpers.wait import wait_until
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

type Behaviour = Callable[[Agent, TurnInput], Awaitable[TurnResult]]


# ------------------------------------------------------------------ fakes


@dataclass
class TurnCall:
    agent: Agent
    turn: TurnInput
    states: StateView | None
    release: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: bool = False


class FakeHistory:
    async def get(self, turn_id: str) -> TurnRecord | None:
        return None


class FakeTurns:
    """A turn component. Each turn waits for ``release`` if ``gated``."""

    def __init__(self) -> None:
        self.calls: list[TurnCall] = []
        self.gated = False
        self.behaviour: Behaviour = ok_text
        self.running = 0
        self.max_running = 0
        self._history = FakeHistory()

    @property
    def history(self) -> FakeHistory:
        return self._history

    async def run_turn(
        self, agent: Agent, turn: TurnInput, *, states: StateView | None = None
    ) -> TurnResult:
        call = TurnCall(agent, turn, states)
        self.calls.append(call)
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        try:
            if self.gated:
                await call.release.wait()
            return await self.behaviour(agent, turn)
        except asyncio.CancelledError:
            call.cancelled = True
            raise
        finally:
            self.running -= 1


async def ok_text(agent: Agent, turn: TurnInput) -> TurnResult:
    return TurnResult(
        turn_id=f"turn-{turn.invocation_id}",
        status="ok",
        result=turn.data.get("text", "done"),
    )


def failing(stop_reason: str, status: str = "failed") -> Behaviour:
    async def behaviour(agent: Agent, turn: TurnInput) -> TurnResult:
        return TurnResult(
            turn_id=f"turn-{turn.invocation_id}",
            status=status,  # type: ignore[arg-type]
            stop_reason=stop_reason,  # type: ignore[arg-type]
            error=f"{stop_reason} happened",
        )

    return behaviour


async def raising(agent: Agent, turn: TurnInput) -> TurnResult:
    msg = "the turns component is not running"
    raise RuntimeError(msg)


@dataclass
class Env:
    h: Harness
    ctx: AppContext
    turns: FakeTurns
    router: RouterComponent

    async def agent(self, **changes: Any) -> Agent:
        return await self.h.agent(**changes)

    async def edit(self, agent: Agent, **changes: Any) -> Agent:
        data = agent.definition.to_data()
        data.update(changes)
        return await self.h.agents.update(
            agent.id, AgentDefinition.from_json(json.dumps(data))
        )

    async def row(self, invocation_id: str) -> dict[str, Any]:
        row = await self.h.db.run(lambda c: router_store.get(c, invocation_id))
        assert row is not None
        return row

    async def restart_router(self, rules: Any = None) -> RouterComponent:
        self.router = RouterComponent(self.turns, rules=rules)
        await self.router.start(self.ctx)
        return self.router


def context(h: Harness, tmp_path: Path) -> AppContext:
    config = Config(ha_url=h.ha.url, ha_token=h.ha.token, data_dir=tmp_path)
    return AppContext(config, h.db, h.client, h.states, "test-installation")


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    async with harness(tmp_path) as h:
        ctx = context(h, tmp_path)
        turns = FakeTurns()
        router = RouterComponent(turns)
        await router.start(ctx)
        value = Env(h, ctx, turns, router)
        try:
            yield value
        finally:
            await value.router.stop()


def req(agent: Agent, text: str = "hi", **fields: Any) -> InvocationRequest:
    fields.setdefault("source", "invoke")
    return InvocationRequest(agent.id, text=text, **fields)


async def started(turns: FakeTurns, count: int) -> None:
    await wait_until(lambda: len(turns.calls) >= count)


# ---------------------------------------------------- AC1: concurrency modes


async def test_queued_runs_in_order_and_refuses_when_full(env: Env) -> None:
    agent = await env.agent(concurrency={"mode": "queued", "queue_size": 2})
    env.turns.gated = True
    handles = [await env.router.submit(req(agent, f"m{i}")) for i in range(3)]
    full = await (await env.router.submit(req(agent, "m3"))).result()
    assert full.status == "error"
    assert full.error_code == "queue_full"

    await started(env.turns, 1)
    for i in range(3):
        assert len(env.turns.calls) == i + 1, "only one invocation runs"
        env.turns.calls[i].release.set()
        result = await handles[i].result()
        assert result.status == "ok"
        assert result.result == f"m{i}"
        if i < 2:
            await started(env.turns, i + 2)
    assert env.turns.max_running == 1
    assert [c.turn.data["text"] for c in env.turns.calls] == ["m0", "m1", "m2"]
    assert (await env.row(full.invocation_id))["error_code"] == "queue_full"


async def test_single_refuses_while_one_runs(env: Env) -> None:
    agent = await env.agent(concurrency={"mode": "single"})
    env.turns.gated = True
    first = await env.router.submit(req(agent, "a"))
    await started(env.turns, 1)
    busy = await env.router.invoke(req(agent, "b"))
    assert busy.status == "error"
    assert busy.error_code == "busy"
    assert len(env.turns.calls) == 1

    env.turns.calls[0].release.set()
    assert (await first.result()).result == "a"
    env.turns.gated = False
    assert (await env.router.invoke(req(agent, "c"))).result == "c"


async def test_restart_cancels_the_running_invocation(env: Env) -> None:
    agent = await env.agent(concurrency={"mode": "restart"})
    env.turns.gated = True
    first = await env.router.submit(req(agent, "old"))
    await started(env.turns, 1)
    second = await env.router.submit(req(agent, "new"))

    old = await first.result()
    assert old.status == "cancelled"
    assert old.error_code == "cancelled"
    assert env.turns.calls[0].cancelled
    await started(env.turns, 2)
    env.turns.calls[1].release.set()
    new = await second.result()
    assert new.status == "ok"
    assert new.result == "new"
    assert (await env.row(old.invocation_id))["status"] == "cancelled"


async def test_parallel_runs_both(env: Env) -> None:
    agent = await env.agent(concurrency={"mode": "parallel", "queue_size": 2})
    env.turns.gated = True
    a = await env.router.submit(req(agent, "a"))
    b = await env.router.submit(req(agent, "b"))
    await started(env.turns, 2)
    assert env.turns.running == 2
    over = await env.router.invoke(req(agent, "c"))
    assert over.error_code == "queue_full"
    for call in env.turns.calls:
        call.release.set()
    assert {(await a.result()).result, (await b.result()).result} == {"a", "b"}


async def test_cancel_stops_a_queued_or_running_invocation(env: Env) -> None:
    agent = await env.agent()
    env.turns.gated = True
    running = await env.router.submit(req(agent, "a"))
    waiting = await env.router.submit(req(agent, "b"))
    await started(env.turns, 1)
    assert await env.router.cancel(waiting.invocation_id)
    assert (await waiting.result()).status == "cancelled"
    assert await env.router.cancel(running.invocation_id)
    assert (await running.result()).status == "cancelled"
    assert not await env.router.cancel("0" * 32)
    assert len(env.turns.calls) == 1


async def test_turned_off_and_unknown_agents_are_refused(env: Env) -> None:
    agent = await env.agent()
    await env.h.agents.set_enabled(agent.id, False)
    off = await env.router.invoke(req(agent))
    assert off.status == "error"
    assert off.error_code == "agent_off"
    assert (await env.row(off.invocation_id))["status"] == "error"
    unknown = await env.router.invoke(InvocationRequest("f" * 32, "invoke"))
    assert unknown.error_code == "agent_not_found"
    assert env.turns.calls == []


async def test_agent_turned_off_while_queued_does_not_run(env: Env) -> None:
    agent = await env.agent()
    env.turns.gated = True
    first = await env.router.submit(req(agent, "a"))
    second = await env.router.submit(req(agent, "b"))
    await started(env.turns, 1)
    await env.h.agents.set_enabled(agent.id, False)
    env.turns.calls[0].release.set()
    assert (await first.result()).status == "ok"
    assert (await second.result()).error_code == "agent_off"
    assert len(env.turns.calls) == 1


class PausingDb:
    """A database wrapper that holds the router's queue insert until
    ``release`` is set. Other calls pass at once."""

    def __init__(self, db: Any) -> None:
        self._db = db
        self.armed = False
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        if self.armed and "insert" in fn.__code__.co_names:
            self.armed = False
            self.waiting.set()
            await self.release.wait()
        return await self._db.run(fn)

    async def close(self) -> None:
        await self._db.close()


async def test_a_job_that_ends_during_submit_keeps_the_lane(env: Env) -> None:
    agent = await env.agent(concurrency={"mode": "queued"})
    await env.router.stop()
    db = PausingDb(env.h.db)
    ctx = AppContext(env.ctx.config, db, env.ctx.ha, env.ctx.states, "x")  # type: ignore[arg-type]
    env.router = RouterComponent(env.turns)
    await env.router.start(ctx)
    env.turns.gated = True

    first = await env.router.submit(req(agent, "a"))
    await started(env.turns, 1)
    db.armed = True
    second_task = asyncio.create_task(env.router.submit(req(agent, "b")))
    await db.waiting.wait()
    # The first invocation ends while the second one is in its insert.
    env.turns.calls[0].release.set()
    assert (await first.result()).result == "a"
    db.release.set()
    second = await second_task
    await started(env.turns, 2)
    third = await env.router.submit(req(agent, "c"))
    # The third invocation waits behind the second one in the same lane.
    lane = env.router._lanes[agent.id]
    assert [j.id for j in lane.waiting] == [third.invocation_id]
    assert [j.id for j in lane.running] == [second.invocation_id]
    env.turns.calls[1].release.set()
    assert (await second.result()).result == "b"
    await started(env.turns, 3)
    env.turns.calls[2].release.set()
    assert (await third.result()).result == "c"
    assert env.turns.max_running == 1


# ------------------------------------------------------- AC2: Assist lanes


async def test_assist_conversations_have_their_own_queues(env: Env) -> None:
    agent = await env.agent(assist={"enabled": True})
    env.turns.gated = True
    background = await env.router.submit(req(agent, "bg"))
    await started(env.turns, 1)

    c1 = await env.router.submit(
        req(agent, "c1-a", source="assist", conversation_id="c1")
    )
    c2 = await env.router.submit(
        req(agent, "c2-a", source="assist", conversation_id="c2")
    )
    c1b = await env.router.submit(
        req(agent, "c1-b", source="assist", conversation_id="c1")
    )
    # The background invocation and both conversations run at once. The
    # second message of conversation c1 waits for the first one.
    await started(env.turns, 3)
    await asyncio.sleep(0)
    texts = [c.turn.data["text"] for c in env.turns.calls]
    assert texts == ["bg", "c1-a", "c2-a"]
    assert env.turns.calls[1].turn.conversation_id == "c1"

    env.turns.calls[1].release.set()
    assert (await c1.result()).result == "c1-a"
    await started(env.turns, 4)
    assert env.turns.calls[3].turn.data["text"] == "c1-b"
    for call in env.turns.calls:
        call.release.set()
    assert (await c1b.result()).result == "c1-b"
    assert (await c2.result()).result == "c2-a"
    assert (await background.result()).result == "bg"
    row = await env.row(c1.invocation_id)
    assert row["queue_key"] == f"{agent.id}/assist:c1"
    assert row["source_key"] == "assist:c1"


# ----------------------------------------- AC3: changes between invocations


class VersionedRules:
    """A rule stage that reads its rule-set version when it starts."""

    def __init__(self) -> None:
        self.version = 1
        self.seen: list[int] = []
        self.entered = 0
        self.gate = asyncio.Event()
        self.gate.set()

    async def try_rules(self, request: RuleRequest) -> RuleOutcome:
        version = self.version  # the snapshot of this invocation
        self.entered += 1
        await self.gate.wait()
        self.seen.append(version)
        return RuleOutcome(
            answered=False,
            reason="no_rule_matched",
            rules_version=version,
            rule_id="r1",
            rule_version=version,
        )


async def test_changes_take_effect_only_between_invocations(env: Env) -> None:
    rules = VersionedRules()
    await env.router.stop()
    await env.restart_router(rules)
    agent = await env.agent()
    assert agent.version == 1

    env.turns.gated = True
    rules.gate.clear()
    first = await env.router.submit(req(agent, "a"))
    second = await env.router.submit(req(agent, "b"))
    await wait_until(lambda: rules.entered == 1)
    # The first invocation started with definition 1 and rule set 1.
    edited = await env.edit(agent, instructions="New instructions.")
    assert edited.version == 2
    rules.version = 2
    rules.gate.set()
    await started(env.turns, 1)
    assert env.turns.calls[0].agent.version == 1
    assert env.turns.calls[0].agent.definition.instructions == "Keep the rooms warm."
    env.turns.calls[0].release.set()
    one = await first.result()
    await started(env.turns, 2)
    assert env.turns.calls[1].agent.version == 2
    env.turns.calls[1].release.set()
    two = await second.result()

    assert rules.seen == [1, 2]
    assert (one.agent_version, one.rule_version) == (1, 1)
    assert (two.agent_version, two.rule_version) == (2, 2)
    row = await env.row(one.invocation_id)
    assert (row["agent_version"], row["rule_id"], row["rule_version"]) == (1, "r1", 1)


async def test_a_rule_answer_skips_the_turn(env: Env) -> None:
    class Answer:
        async def try_rules(self, request: RuleRequest) -> RuleOutcome:
            return RuleOutcome(
                answered=True, result={"target": 20}, rule_id="r9", rule_version=3
            )

    await env.router.stop()
    await env.restart_router(Answer())
    agent = await env.agent()
    result = await env.router.invoke(req(agent))
    assert (result.status, result.path, result.result) == ("ok", "rule", {"target": 20})
    assert (result.rule_id, result.rule_version) == ("r9", 3)
    assert env.turns.calls == []
    # Review and Assist invocations always start an agent turn.
    review = await env.router.invoke(req(agent, source="review"))
    assert review.path == "agent_turn"


# ----------------------------------------------------------- AC4: fallback

CAUSES = [
    ("budget", "budget"),
    ("provider_error", "provider_unreachable"),
    ("tool_call_limit", "tool_call_limit"),
    ("token_limit", "token_limit"),
    ("error", "turn_failed"),
]


@pytest.mark.parametrize(("stop_reason", "reason"), CAUSES)
async def test_fallback_modes_for_each_cause(
    env: Env, stop_reason: str, reason: str
) -> None:
    status = "stopped" if stop_reason.endswith("_limit") else "failed"
    agents = {
        "last_result": await env.agent(name="last"),
        "fixed": await env.agent(
            name="fixed", fallback={"mode": "fixed", "value": {"target": 18}}
        ),
        "no_change": await env.agent(name="none", fallback={"mode": "no_change"}),
        "error": await env.agent(name="err", fallback={"mode": "error"}),
    }
    # One earlier result for the last_result agent, from the same source.
    assert (await env.router.invoke(req(agents["last_result"], "earlier"))).ok
    env.turns.behaviour = failing(stop_reason, status)

    results: dict[str, InvocationResult] = {}
    for mode, agent in agents.items():
        results[mode] = await env.router.invoke(req(agent, "now"))

    expected = {
        "last_result": ("fallback", "earlier", None),
        "fixed": ("fallback", {"target": 18}, None),
        "no_change": ("fallback", None, None),
        "error": ("error", None, "fallback_error"),
    }
    for mode, (status_, value, code) in expected.items():
        result = results[mode]
        assert (result.status, result.result, result.error_code) == (
            status_,
            value,
            code,
        ), mode
        assert result.path == "fallback"
        assert result.fallback_reason == reason
        row = await env.row(result.invocation_id)
        assert row["path"] == "fallback"
        assert row["fallback_reason"] == reason
        assert row["turn_id"] == f"turn-{result.invocation_id}"


async def test_last_result_without_an_earlier_result_is_an_error(env: Env) -> None:
    agent = await env.agent()
    # A result from another source does not count.
    assert (await env.router.invoke(req(agent, "x", source="schedule"))).ok
    env.turns.behaviour = failing("budget")
    result = await env.router.invoke(req(agent))
    assert result.status == "error"
    assert result.error_code == "no_last_result"
    assert result.fallback_reason == "budget"
    # A fallback result does not become the next last result.
    again = await env.router.invoke(req(agent))
    assert again.error_code == "no_last_result"


async def test_a_turn_that_cannot_start_returns_the_fallback(env: Env) -> None:
    agent = await env.agent(fallback={"mode": "no_change"})
    env.turns.behaviour = raising
    result = await env.router.invoke(req(agent))
    assert result.status == "fallback"
    assert result.fallback_reason == "runtime_unavailable"
    assert result.error is not None
    assert "not running" in result.error


# ------------------------------------------------------ AC5: restart safety


async def _insert(env: Env, agent: Agent, invocation_id: str, text: str) -> None:
    request = req(agent, text)
    await env.h.db.run(
        lambda c: router_store.insert(c, invocation_id, request, status="queued")
    )


async def test_crash_resumes_queued_and_fails_running(env: Env) -> None:
    agent = await env.agent(fallback={"mode": "fixed", "value": "safe"})
    await env.router.stop()
    # The state that a crash leaves: one running row and two queued rows.
    await _insert(env, agent, "a" * 32, "was running")
    await env.h.db.run(lambda c: router_store.mark_running(c, "a" * 32, 1))
    await _insert(env, agent, "b" * 32, "queued 1")
    await _insert(env, agent, "c" * 32, "queued 2")

    seen: list[InvocationResult] = []
    router = RouterComponent(env.turns)
    router.add_result_listener(seen.append)
    env.router = router
    await router.start(env.ctx)
    await wait_until(lambda: len(seen) == 3)

    by_id = {r.invocation_id: r for r in seen}
    crashed = by_id["a" * 32]
    assert (crashed.status, crashed.path, crashed.result) == (
        "fallback",
        "fallback",
        "safe",
    )
    assert crashed.fallback_reason == "app_restart"
    assert [c.turn.data["text"] for c in env.turns.calls] == ["queued 1", "queued 2"]
    assert by_id["b" * 32].result == "queued 1"
    assert (await env.row("a" * 32))["status"] == "fallback"
    assert (await env.row("c" * 32))["status"] == "ok"


async def test_stop_fails_running_and_keeps_queued(env: Env) -> None:
    agent = await env.agent(fallback={"mode": "no_change"})
    env.turns.gated = True
    running = await env.router.submit(req(agent, "a"))
    queued = await env.router.submit(req(agent, "b"))
    await started(env.turns, 1)
    await env.router.stop()

    stopped = await running.result()
    assert (stopped.status, stopped.fallback_reason) == ("fallback", "app_stopped")
    assert env.turns.calls[0].cancelled
    assert (await queued.result()).error_code == "not_running"
    assert (await env.row(queued.invocation_id))["status"] == "queued"

    env.turns.gated = False
    seen: list[InvocationResult] = []
    router = RouterComponent(env.turns)
    router.add_result_listener(seen.append)
    env.router = router
    await router.start(env.ctx)
    await wait_until(lambda: len(seen) == 1)
    assert seen[0].invocation_id == queued.invocation_id
    assert seen[0].result == "b"


# ------------------------------------------- AC6: records, cost and retries


async def test_every_invocation_records_path_result_and_versions(env: Env) -> None:
    agent = await env.agent()
    result = await env.router.invoke(req(agent, "hello", caller={"automation": "x"}))
    row = await env.row(result.invocation_id)
    assert row["status"] == "ok"
    assert row["path"] == "agent_turn"
    assert row["result"] == "hello"
    assert row["agent_version"] == 1
    assert row["rule_id"] is None
    assert row["cost_pusd"] == 0
    assert row["turn_id"] == f"turn-{result.invocation_id}"
    assert row["request"]["caller"] == {"automation": "x"}
    assert row["started_at"] is not None
    assert row["finished_at"] is not None
    data = result.to_data()
    assert data["path"] == "agent_turn"
    assert data["cost_usd"] == 0.0


PRICES = {"openai:chat": {"input": 1.0, "output": 2.0}}


@dataclass
class Flaky:
    """A FunctionModel that fails ``failures`` times with HTTP 503."""

    failures: int
    calls: int = 0

    def model(self) -> FunctionModel:
        def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            self.calls += 1
            if self.calls <= self.failures:
                raise ModelHTTPError(503, "chat", body="unavailable")
            return ModelResponse(
                parts=[TextPart("warm enough")],
                usage=RequestUsage(input_tokens=1000, output_tokens=100),
            )

        return FunctionModel(respond, model_name="chat")


async def _provider_calls(env: Env) -> list[dict[str, Any]]:
    def run(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = conn.execute("SELECT * FROM provider_calls ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    return await env.h.db.run(run)


async def test_cost_links_provider_calls_and_chat_retries(env: Env) -> None:
    config = PConfig(price_overrides=PRICES)
    meter = Meter(env.h.db, config, PriceTable(PRICES), ConfigKeySource(config))
    flaky = Flaky(failures=2)
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    def model_for(ref: Any, agent_id: str) -> Any:
        metered = MeteredModel(
            flaky.model(),
            meter,
            provider="openai",
            agent_id=agent_id,
            max_output_tokens=100,
        )
        return RetryingModel(metered, RetryPolicy(attempts=3), sleep=sleep)

    history = TurnHistory(env.h.db)
    runner = TurnRunner(lambda: env.h.executor, history, model_for)

    class RealTurns:
        @property
        def history(self) -> TurnHistory:
            return history

        async def run_turn(
            self, agent: Agent, turn: TurnInput, *, states: StateView | None = None
        ) -> TurnResult:
            return await runner.run_turn(agent, turn, states=states)

    await env.router.stop()
    env.router = RouterComponent(RealTurns())
    await env.router.start(env.ctx)
    agent = await env.agent(result={"type": "text"})
    result = await env.router.invoke(req(agent, "warm?"))
    await runner.close()

    assert result.status == "ok"
    assert result.result == "warm enough"
    calls = await _provider_calls(env)
    assert [c["status"] for c in calls] == ["error", "error", "ok"]
    assert {c["invocation_id"] for c in calls} == {result.invocation_id}
    assert sleeps == [1.0, 2.0]
    # 1000 input tokens at USD 1/M plus 100 output tokens at USD 2/M.
    assert result.cost_usd == Decimal("0.0012")
    assert (await env.row(result.invocation_id))["cost_pusd"] == sum(
        c["cost_pusd"] for c in calls
    )


async def test_chat_retry_gives_up_and_returns_the_fallback(env: Env) -> None:
    flaky = Flaky(failures=5)

    async def sleep(delay: float) -> None:
        return None

    history = TurnHistory(env.h.db)
    runner = TurnRunner(
        lambda: env.h.executor,
        history,
        lambda ref, agent_id: RetryingModel(
            flaky.model(), RetryPolicy(attempts=3), sleep=sleep
        ),
    )

    class RealTurns:
        @property
        def history(self) -> TurnHistory:
            return history

        async def run_turn(
            self, agent: Agent, turn: TurnInput, *, states: StateView | None = None
        ) -> TurnResult:
            return await runner.run_turn(agent, turn, states=states)

    await env.router.stop()
    env.router = RouterComponent(RealTurns())
    await env.router.start(env.ctx)
    agent = await env.agent(fallback={"mode": "fixed", "value": "offline"})
    result = await env.router.invoke(req(agent))
    await runner.close()
    assert flaky.calls == 3
    assert (result.status, result.result) == ("fallback", "offline")
    assert result.fallback_reason == "provider_unreachable"


def test_retryable_errors() -> None:
    assert retryable(ModelHTTPError(503, "m"))
    assert retryable(ModelHTTPError(429, "m"))
    assert not retryable(ModelHTTPError(400, "m"))
    assert not retryable(ModelHTTPError(401, "m"))
    assert retryable(ProviderHTTPError("down"))
    assert not retryable(ProviderHTTPError("bad", status=400))
    assert not retryable(BudgetExceededError("spent", "agent"))
    assert not retryable(ValueError("x"))
    assert RetryPolicy(base_delay=1, max_delay=3).delay(5) == 3


async def test_router_refuses_before_start(tmp_path: Path) -> None:
    router = RouterComponent(FakeTurns())
    with pytest.raises(RouterNotRunningError):
        await router.submit(InvocationRequest("a" * 32, "invoke"))


def test_request_validation_and_json() -> None:
    with pytest.raises(ValueError, match="unknown invocation source"):
        InvocationRequest("a" * 32, "nope")
    with pytest.raises(ValueError, match="conversation ID"):
        InvocationRequest("a" * 32, "assist")
    request = InvocationRequest(
        "a" * 32,
        "ai_task",
        text="t",
        data={"k": 1},
        result_schema={"type": "object"},
        caller={"task_name": "n"},
    )
    assert InvocationRequest.from_json(request.to_json()) == request
    assert request.turn_data == {"k": 1, "text": "t"}
