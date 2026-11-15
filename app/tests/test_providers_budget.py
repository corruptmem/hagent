"""Budget and reservation tests."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from hagent_app.providers import (
    BudgetExceededError,
    CallSpec,
    DecisionModel,
    DecisionRequest,
    PriceUnknownError,
    TokenUsage,
    YesNoQuestion,
)
from helpers.fake_providers import Reply, fixture, route
from helpers.providers_env import ProvidersEnv
from helpers.wait import wait_until

StartEnv = Callable[..., Awaitable[ProvidersEnv]]

# gpt-4o-mini input costs USD 0.15 per million tokens.
MILLION_INPUT = TokenUsage(input_tokens=1_000_000)


def _spec(agent_id: str | None, model: str = "gpt-4o-mini") -> CallSpec:
    return CallSpec(agent_id, "openai", "chat", model, "openai")


class Clock:
    """A clock that tests can move."""

    def __init__(self) -> None:
        self.now = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


async def _parallel_calls(
    env: ProvidersEnv, agents: list[str | None]
) -> tuple[int, int, int]:
    """Start one held call per agent. Return (entered, refused, peak held)."""
    meter = env.component.meter
    gate = asyncio.Event()
    entered = 0

    async def one(agent_id: str | None) -> None:
        nonlocal entered
        async with meter.call(_spec(agent_id), MILLION_INPUT) as call:
            entered += 1
            await gate.wait()
            call.complete(MILLION_INPUT)

    tasks = [asyncio.create_task(one(a)) for a in agents]
    await wait_until(lambda: sum(t.done() for t in tasks) + entered == len(agents))
    held = await env.reservations()
    gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    refused = sum(isinstance(r, BudgetExceededError) for r in results)
    assert all(r is None or isinstance(r, BudgetExceededError) for r in results)
    return entered, refused, held


async def test_parallel_calls_cannot_overspend_agent_budget(
    providers_env: StartEnv,
) -> None:
    env = await providers_env(agent_daily_budget_usd=0.50)
    entered, refused, held = await _parallel_calls(env, ["a1"] * 10)
    # Each call reserves USD 0.15, so three fit in USD 0.50.
    assert (entered, refused, held) == (3, 7, 3)
    state = await env.component.meter.budget_state("a1")
    assert state.spent_usd == Decimal("0.45")
    assert state.reserved_usd == 0
    assert state.remaining_usd == Decimal("0.05")
    assert await env.reservations() == 0


async def test_installation_budget_covers_all_agents_and_none(
    providers_env: StartEnv,
) -> None:
    env = await providers_env(
        agent_daily_budget_usd=0.50, installation_daily_budget_usd=0.40
    )
    entered, refused, _ = await _parallel_calls(env, ["a1", "a2", None, "a3"])
    assert (entered, refused) == (2, 2)
    with pytest.raises(BudgetExceededError) as info:
        await env.component.meter.reserve(_spec("a4"), 200_000_000_000, priced=True)
    assert info.value.scope == "installation"
    state = await env.component.meter.budget_state(None)
    assert state.spent_usd == Decimal("0.30")


async def test_per_agent_budget_override(providers_env: StartEnv) -> None:
    env = await providers_env(agent_budgets_usd={"big": 2.0})
    meter = env.component.meter
    assert await meter.agent_budget_usd("big") == Decimal("2.0")
    assert await meter.agent_budget_usd("other") == Decimal("0.5")
    meter.set_agent_budgets({"other": 0.1})
    with pytest.raises(BudgetExceededError) as info:
        async with meter.call(_spec("other"), MILLION_INPUT):
            pass
    assert info.value.scope == "agent"


async def test_real_cost_above_reservation_counts(providers_env: StartEnv) -> None:
    env = await providers_env(agent_daily_budget_usd=0.50)
    meter = env.component.meter
    async with meter.call(_spec("a1"), TokenUsage(input_tokens=10)) as call:
        call.complete(TokenUsage(input_tokens=3_000_000))  # USD 0.45
    with pytest.raises(BudgetExceededError):
        async with meter.call(_spec("a1"), MILLION_INPUT):
            pass


async def test_budget_resets_on_the_next_local_day(providers_env: StartEnv) -> None:
    clock = Clock()
    env = await providers_env(clock=clock, agent_daily_budget_usd=0.20)
    meter = env.component.meter
    async with meter.call(_spec("a1"), MILLION_INPUT) as call:
        call.complete(MILLION_INPUT)
    with pytest.raises(BudgetExceededError):
        async with meter.call(_spec("a1"), MILLION_INPUT):
            pass
    clock.now += timedelta(days=1)
    async with meter.call(_spec("a1"), MILLION_INPUT) as call:
        call.complete(MILLION_INPUT)
    days = [row["budget_day"] for row in await env.calls()]
    assert days == ["2026-11-14", "2026-11-15"]


async def test_failed_call_releases_its_reservation(providers_env: StartEnv) -> None:
    env = await providers_env()
    meter = env.component.meter
    with pytest.raises(RuntimeError, match="boom"):
        async with meter.call(_spec("a1"), MILLION_INPUT):
            assert await env.reservations() == 1
            raise RuntimeError("boom")
    assert await env.reservations() == 0
    [row] = await env.calls()
    assert row["status"] == "error"
    assert row["error"] == "RuntimeError: boom"


async def test_cancelled_call_releases_its_reservation(
    providers_env: StartEnv,
) -> None:
    env = await providers_env()
    meter = env.component.meter
    started = asyncio.Event()

    async def hold() -> None:
        async with meter.call(_spec("a1"), MILLION_INPUT):
            started.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(hold())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await env.reservations() == 0


async def test_start_clears_reservations_of_last_run(
    providers_env: StartEnv,
) -> None:
    env = await providers_env()

    def insert(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO budget_reservations"
            " (created_at, budget_day, agent_id, amount_pusd)"
            " VALUES ('x', '2026-11-14', 'a1', 5)"
        )

    await env.db.run(insert)
    assert await env.component.meter.clear_reservations() == 1
    assert await env.reservations() == 0


async def test_unknown_price_refuses_call_before_http(
    providers_env: StartEnv,
) -> None:
    env = await providers_env()
    env.fake.reply(route("openai", "/decisions"), Reply("decision-openai"))
    request = DecisionRequest(
        DecisionModel("openai", "no-such-model"), "x", [YesNoQuestion("q", "?")]
    )
    with pytest.raises(PriceUnknownError):
        await env.component.decisions.decide(request, agent_id="a1")
    assert env.fake.seen == []


async def test_unknown_price_with_reserve_option(providers_env: StartEnv) -> None:
    env = await providers_env(unknown_price_reserve_usd=0.01)
    body = fixture("decision-openai")["response"]
    body["answers"] = [{"type": "predicate", "name": "q", "probability": 0.5}]
    env.fake.reply(route("openai", "/decisions"), Reply(json_body=body))
    request = DecisionRequest(
        DecisionModel("openai", "no-such-model"), "x", [YesNoQuestion("q", "?")]
    )
    await env.component.decisions.decide(request, agent_id="a1")
    [row] = await env.calls()
    assert row["cost_source"] == "reserved"
    assert row["cost_pusd"] == row["reserved_pusd"] == 10_000_000_000
