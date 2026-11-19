"""Tests of the scheduler (task-15): schedules, reviews and subscriptions.

The tests run the real tools component and the real scheduler against
``FakeHA`` with the sample home. A fake router records each submitted
invocation. ``Clock`` is the clock seam: the tests move it to simulate an
app that was stopped while a schedule was due. No test needs a live HA
instance or a provider.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from hagent_app.agents import Agent, AgentDefinition
from hagent_app.agents.service import AgentService
from hagent_app.app import App, AppContext
from hagent_app.config import Config
from hagent_app.ha import HAClient
from hagent_app.http.events import ChangeEvents
from hagent_app.router import InvocationRequest, InvocationResult, RouterNotRunningError
from hagent_app.scheduler import SchedulerComponent, contains_secret, redact
from hagent_app.scheduler import store as sched_store
from hagent_app.scheduler.cron import cron_trigger, min_gap_seconds
from hagent_app.tools import ToolContext, ToolResult, ToolsComponent
from helpers.agents import definition
from helpers.fake_ha import FakeHA
from helpers.home import populate
from helpers.wait import wait_until

HEX = "0123456789abcdef" * 2
READ_HALL = {
    "tools__self": True,
    "tools__ha": {"level": "read", "scope": {"entities": ["light.hall"]}},
}
CONTROL_HALL = {
    "tools__self": True,
    "tools__ha": {"level": "control", "scope": {"entities": ["light.hall"]}},
}

# ------------------------------------------------------------------ fakes


class Clock:
    """The clock seam: real time plus an offset, or a fixed time."""

    def __init__(self) -> None:
        self.offset = timedelta()
        self.fixed: datetime | None = None

    def __call__(self) -> datetime:
        if self.fixed is not None:
            return self.fixed
        return datetime.now(UTC) + self.offset


@dataclass
class FakeHandle:
    invocation_id: str
    future: asyncio.Future[InvocationResult]

    async def result(self) -> InvocationResult:
        return await self.future


@dataclass
class FakeRouter:
    """Records each request. ``refuse`` sets an error code for the result."""

    requests: list[InvocationRequest] = field(default_factory=list)
    refuse: str | None = None
    down: bool = False

    async def submit(self, request: InvocationRequest) -> FakeHandle:
        if self.down:
            msg = "the router is not running"
            raise RouterNotRunningError(msg)
        self.requests.append(request)
        invocation_id = uuid.uuid4().hex
        future: asyncio.Future[InvocationResult] = (
            asyncio.get_running_loop().create_future()
        )
        future.set_result(
            InvocationResult(
                invocation_id,
                request.agent_id,
                "error" if self.refuse else "ok",
                error_code=self.refuse,
            )
        )
        return FakeHandle(invocation_id, future)

    def of(self, source: str) -> list[InvocationRequest]:
        return [r for r in self.requests if r.source == source]


@dataclass
class Env:
    ha: FakeHA
    ctx: AppContext
    tools: ToolsComponent
    scheduler: SchedulerComponent
    router: FakeRouter
    agents: AgentService
    clock: Clock

    async def agent(self, **changes: Any) -> Agent:
        changes.setdefault("name", f"Agent {uuid.uuid4().hex[:6]}")
        data = definition(**changes)
        return await self.agents.create(AgentDefinition.from_json(json.dumps(data)))

    async def update(self, agent: Agent, **changes: Any) -> Agent:
        """Save the current definition with changes (``__`` names a nested key)."""
        current = await self.agents.get(agent.id)
        data = json.loads(current.definition.model_dump_json(by_alias=True))
        for key, value in changes.items():
            target = data
            *parents, last = key.split("__")
            for part in parents:
                target = target.setdefault(part, {})
            target[last] = value
        return await self.agents.update(
            agent.id, AgentDefinition.from_json(json.dumps(data))
        )

    async def call(
        self, agent: Agent, tool: str, args: Mapping[str, Any]
    ) -> ToolResult:
        current = await self.agents.get(agent.id)
        ctx = ToolContext(current, invocation_id="inv-1")
        return await self.tools.executor().execute(ctx, tool, args)

    async def create(self, agent: Agent, kind: str, spec: Mapping[str, Any]) -> str:
        result = await self.call(agent, "self.create", {"kind": kind, "spec": spec})
        assert result.ok, result.error
        return str(result.data["item_id"])

    async def runs(self, agent: Agent) -> list[dict[str, Any]]:
        return await self.ctx.db.run(lambda c: sched_store.list_runs(c, agent.id))

    async def events(self, agent: Agent) -> list[dict[str, Any]]:
        return await self.ctx.db.run(lambda c: sched_store.list_events(c, agent.id))

    def job(self, job_id: str) -> Any:
        scheduler = self.scheduler._scheduler
        assert scheduler is not None
        return scheduler.get_job(job_id)


@asynccontextmanager
async def scheduler_env(
    ha: FakeHA,
    tmp_path: Path,
    *,
    clock: Clock | None = None,
    router: FakeRouter | None = None,
) -> AsyncIterator[Env]:
    config = Config(
        ha_url=ha.url,
        ha_token=ha.token,
        data_dir=tmp_path,
        port=0,
        options={"scheduler": {"timezone": "UTC", "own_write_hold_seconds": 1.0}},
    )
    events = ChangeEvents()
    tools = ToolsComponent(events)
    router = router or FakeRouter()
    clock = clock or Clock()
    scheduler = SchedulerComponent(router, tools, events, clock=clock)
    tools.agent_state = scheduler.state_store
    tools.calls = scheduler.calls
    client = HAClient(ha.url, ha.token, backoff_initial=0.05, backoff_max=0.1)
    app = App(config, [tools, scheduler], client=client)
    ctx = await app.start()
    try:
        registry = tools.registry
        assert registry is not None
        await wait_until(lambda: ctx.states.ready and not registry.stale)
        yield Env(
            ha, ctx, tools, scheduler, router, AgentService(ctx.db, events), clock
        )
    finally:
        await app.stop()


async def eventually(check: Callable[[], Awaitable[bool]], limit: float = 4.0) -> None:
    """Poll the async ``check`` until it is true. Fail after ``limit`` seconds."""
    async with asyncio.timeout(limit):
        while not await check():  # noqa: ASYNC110 - polls the database
            await asyncio.sleep(0.01)


@pytest.fixture
def home(fake_ha: FakeHA) -> FakeHA:
    populate(fake_ha)
    return fake_ha


# ------------------------------------------------------------- AC 1


async def test_schedules_and_subscriptions_persist_across_a_restart(
    home: FakeHA, tmp_path: Path
) -> None:
    async with scheduler_env(home, tmp_path) as env:
        agent = await env.agent(scheduling__debounce_seconds=0, **READ_HALL)
        timer = await env.create(
            agent, "schedule", {"type": "timer", "delay_seconds": 3600}
        )
        cron = await env.create(
            agent, "schedule", {"type": "cron", "cron": "0 4 * * *"}
        )
        sub = await env.create(agent, "subscription", {"entity_ids": ["light.hall"]})
        before = await env.call(agent, "self.list", {"kind": "schedule"})
        assert before.ok
        next_runs = {i["item_id"]: i["next_run_at"] for i in before.data}
        assert next_runs[timer] is not None
        assert next_runs[cron] is not None
        assert next_runs[cron].endswith("04:00:00+00:00")

    async with scheduler_env(home, tmp_path) as env:
        after = await env.call(agent, "self.list", {"kind": "schedule"})
        assert {i["item_id"]: i["next_run_at"] for i in after.data} == next_runs
        subs = await env.call(agent, "self.list", {"kind": "subscription"})
        assert [i["item_id"] for i in subs.data] == [sub]
        # The restored subscription receives events again.
        await home.set_state("light.hall", "off")
        await wait_until(lambda: len(env.router.of("event")) == 1, 4.0)
        request = env.router.of("event")[0]
        assert request.caller == {"subscription_id": sub}
        assert request.data["changes"][0]["entity_id"] == "light.hall"


async def test_a_timer_fires_through_apscheduler(home: FakeHA, tmp_path: Path) -> None:
    async with scheduler_env(home, tmp_path) as env:
        agent = await env.agent(scheduling__min_interval_seconds=1, **READ_HALL)
        timer = await env.create(
            agent,
            "schedule",
            {"type": "timer", "delay_seconds": 1, "note": "check", "data": {"a": 1}},
        )
        await wait_until(lambda: len(env.router.requests) == 1, 5.0)
        request = env.router.requests[0]
        assert request.source == "schedule"
        assert request.caller == {"schedule_id": timer}
        assert request.data["schedule"]["note"] == "check"
        assert request.data["data"] == {"a": 1}
        # A timer runs once; the app then deletes it.
        await wait_until(lambda: env.job(f"item:{timer}") is None)
        listed = await env.call(agent, "self.list", {"kind": "schedule"})
        assert listed.data == []
        runs = await env.runs(agent)
        assert [r["status"] for r in runs] == ["submitted"]


async def test_a_review_timer_uses_the_review_source(
    home: FakeHA, tmp_path: Path
) -> None:
    async with scheduler_env(home, tmp_path) as env:
        agent = await env.agent(scheduling__min_interval_seconds=1, **READ_HALL)
        await env.create(
            agent, "schedule", {"type": "timer", "delay_seconds": 1, "review": True}
        )
        await wait_until(lambda: len(env.router.requests) == 1, 5.0)
        assert env.router.requests[0].source == "review"


# ------------------------------------------------------------- AC 2


async def test_max_schedules_counts_schedules_and_subscriptions(
    home: FakeHA, tmp_path: Path
) -> None:
    async with scheduler_env(home, tmp_path) as env:
        agent = await env.agent(scheduling__max_schedules=2, **READ_HALL)
        await env.create(agent, "schedule", {"type": "cron", "cron": "0 4 * * *"})
        await env.create(agent, "subscription", {"entity_ids": ["light.hall"]})
        result = await env.call(
            agent,
            "self.create",
            {"kind": "schedule", "spec": {"type": "cron", "cron": "0 5 * * *"}},
        )
        assert result.error_code == "limit_reached"
        listed = await env.call(agent, "self.list", {"kind": "schedule"})
        assert len(listed.data) == 1


@pytest.mark.parametrize(
    ("spec", "code"),
    [
        ({"type": "cron", "cron": "* * * * *"}, "min_interval"),
        ({"type": "cron", "cron": "0,1 * * * *"}, "min_interval"),
        ({"type": "cron", "cron": "0 0 30 2 *"}, "never_fires"),
        ({"type": "timer", "delay_seconds": 60}, "min_interval"),
    ],
)
async def test_min_interval_is_enforced_on_create(
    home: FakeHA, tmp_path: Path, spec: dict[str, Any], code: str
) -> None:
    async with scheduler_env(home, tmp_path) as env:
        agent = await env.agent(scheduling__min_interval_seconds=120, **READ_HALL)
        result = await env.call(
            agent, "self.create", {"kind": "schedule", "spec": spec}
        )
        assert result.error_code == code
        ok = await env.call(
            agent,
            "self.create",
            {"kind": "schedule", "spec": {"type": "cron", "cron": "*/5 * * * *"}},
        )
        assert ok.ok, ok.error


async def test_a_timer_at_a_time_too_soon_is_refused(
    home: FakeHA, tmp_path: Path
) -> None:
    async with scheduler_env(home, tmp_path) as env:
        agent = await env.agent(**READ_HALL)
        soon = (datetime.now(UTC) + timedelta(seconds=10)).isoformat()
        result = await env.call(
            agent,
            "self.create",
            {"kind": "schedule", "spec": {"type": "timer", "at": soon}},
        )
        assert result.error_code == "min_interval"


async def test_min_interval_is_enforced_at_fire_time(
    home: FakeHA, tmp_path: Path
) -> None:
    async with scheduler_env(home, tmp_path) as env:
        agent = await env.agent(scheduling__min_interval_seconds=60, **READ_HALL)
        first = await env.create(
            agent, "schedule", {"type": "timer", "delay_seconds": 600}
        )
        second = await env.create(
            agent, "schedule", {"type": "timer", "delay_seconds": 601}
        )
        await env.scheduler.fire(f"item:{first}")
        await env.scheduler.fire(f"item:{second}")
        assert len(env.router.requests) == 1
        runs = await env.runs(agent)
        assert [r["status"] for r in runs] == ["submitted", "skipped_min_interval"]


async def test_debounce_merges_changes_and_the_definition_is_a_floor(
    home: FakeHA, tmp_path: Path
) -> None:
    async with scheduler_env(home, tmp_path) as env:
        agent = await env.agent(scheduling__debounce_seconds=0.4, **READ_HALL)
        sub = await env.create(
            agent, "subscription", {"entity_ids": ["light.hall"], "debounce_seconds": 0}
        )
        listed = await env.call(agent, "self.list", {"kind": "subscription"})
        assert listed.data[0]["debounce_seconds"] == 0.4
        for value in ("off", "on", "off"):
            await home.set_state("light.hall", value)
        await wait_until(lambda: len(env.router.requests) == 1, 4.0)
        await asyncio.sleep(0.6)
        assert len(env.router.requests) == 1
        data = env.router.requests[0].data
        assert data["subscription"]["id"] == sub
        assert data["count"] == 3
        assert [c["new_state"]["state"] for c in data["changes"]] == [
            "off",
            "on",
            "off",
        ]
        rows = await env.events(agent)
        assert len(rows) == 3
        assert {r["outcome"] for r in rows} == {"delivered"}
        assert len({r["invocation_id"] for r in rows}) == 1


async def test_a_longer_spec_debounce_wins(home: FakeHA, tmp_path: Path) -> None:
    async with scheduler_env(home, tmp_path) as env:
        agent = await env.agent(scheduling__debounce_seconds=0.1, **READ_HALL)
        await env.create(
            agent,
            "subscription",
            {"entity_ids": ["light.hall"], "debounce_seconds": 30},
        )
        listed = await env.call(agent, "self.list", {"kind": "subscription"})
        assert listed.data[0]["debounce_seconds"] == 30


# --------------------------------------------------------- AC 3 and 4


async def test_own_writes_are_ignored_and_history_marks_the_origin(
    home: FakeHA, tmp_path: Path
) -> None:
    async with scheduler_env(home, tmp_path) as env:
        agent = await env.agent(scheduling__debounce_seconds=0, **CONTROL_HALL)
        await env.create(agent, "subscription", {"entity_ids": ["light.hall"]})
        home.states["light.hall"]["state"] = "off"
        result = await env.call(
            agent,
            "ha.call_service",
            {
                "domain": "light",
                "service": "turn_on",
                "target": {"entity_id": "light.hall"},
            },
        )
        assert result.ok, result.error
        own_context = result.data["context_id"]
        tracker = env.scheduler.tracker
        assert tracker is not None
        await wait_until(lambda: tracker.held == 0)
        await asyncio.sleep(0.2)
        assert env.router.requests == []
        # A person changes the light: the agent receives it as external.
        await home.set_state("light.hall", "off")
        await wait_until(lambda: len(env.router.requests) == 1, 4.0)
        change = env.router.requests[0].data["changes"][0]
        assert change["origin"] == "external"
        assert change["own"] is False
        rows = await env.events(agent)
        assert [(r["outcome"], r["origin"]) for r in rows] == [
            ("own_write", "agent"),
            ("delivered", "external"),
        ]
        assert rows[0]["origin_agent_id"] == agent.id
        assert rows[0]["context_id"] == own_context
        assert rows[1]["origin_agent_id"] is None


async def test_include_own_writes_delivers_them_marked_as_agent(
    home: FakeHA, tmp_path: Path
) -> None:
    async with scheduler_env(home, tmp_path) as env:
        agent = await env.agent(scheduling__debounce_seconds=0, **CONTROL_HALL)
        await env.create(
            agent,
            "subscription",
            {"entity_ids": ["light.hall"], "include_own_writes": True},
        )
        home.states["light.hall"]["state"] = "off"
        await env.call(
            agent,
            "ha.call_service",
            {
                "domain": "light",
                "service": "turn_on",
                "target": {"entity_id": "light.hall"},
            },
        )
        await wait_until(lambda: len(env.router.requests) == 1, 4.0)
        change = env.router.requests[0].data["changes"][0]
        assert change["origin"] == "agent"
        assert change["own"] is True


async def test_a_write_by_another_agent_is_not_an_own_write(
    home: FakeHA, tmp_path: Path
) -> None:
    async with scheduler_env(home, tmp_path) as env:
        watcher = await env.agent(scheduling__debounce_seconds=0, **READ_HALL)
        writer = await env.agent(**CONTROL_HALL)
        await env.create(watcher, "subscription", {"entity_ids": ["light.hall"]})
        home.states["light.hall"]["state"] = "off"
        await env.call(
            writer,
            "ha.call_service",
            {
                "domain": "light",
                "service": "turn_on",
                "target": {"entity_id": "light.hall"},
            },
        )
        await wait_until(lambda: len(env.router.requests) == 1, 4.0)
        request = env.router.requests[0]
        assert request.agent_id == watcher.id
        assert request.data["changes"][0]["origin"] == "agent"
        assert request.data["changes"][0]["own"] is False
        rows = await env.events(watcher)
        assert rows[0]["origin_agent_id"] == writer.id


async def test_a_failed_service_call_never_blocks_the_tracker(
    home: FakeHA, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with scheduler_env(home, tmp_path) as env:
        agent = await env.agent(**CONTROL_HALL)

        async def fail(*args: Any, **kwargs: Any) -> Any:
            msg = "boom"
            raise ConnectionError(msg)

        monkeypatch.setattr(env.ctx.ha, "call_service", fail)
        result = await env.call(
            agent,
            "ha.call_service",
            {
                "domain": "light",
                "service": "turn_on",
                "target": {"entity_id": "light.hall"},
            },
        )
        assert not result.ok
        assert env.scheduler.tracker is not None
        assert env.scheduler.tracker.in_flight == 0


# ------------------------------------------------- delivery-time checks


async def test_a_subscription_outside_the_current_scope_is_not_delivered(
    home: FakeHA, tmp_path: Path
) -> None:
    async with scheduler_env(home, tmp_path) as env:
        wide = {"level": "read", "scope": {"entities": ["light.hall", "light.kitchen"]}}
        agent = await env.agent(
            tools__self=True, tools__ha=wide, scheduling__debounce_seconds=0
        )
        await env.create(agent, "subscription", {"entity_ids": ["light.kitchen"]})
        # The user narrows the scope. The subscription stays, but delivers nothing.
        await env.update(
            agent,
            tools__self=True,
            scheduling__debounce_seconds=0,
            **{"tools__ha": {"level": "read", "scope": {"entities": ["light.hall"]}}},
        )
        await home.set_state("light.kitchen", "off")
        await wait_until(lambda: env.ha.states["light.kitchen"]["state"] == "off")

        async def denied() -> bool:
            rows = await env.events(agent)
            return bool(rows) and rows[0]["outcome"] == "denied"

        await eventually(denied, 4)
        assert env.router.requests == []
        rows = await env.events(agent)
        assert rows[0]["detail"] == "out_of_scope"


async def test_a_turned_off_agent_gets_no_events(home: FakeHA, tmp_path: Path) -> None:
    async with scheduler_env(home, tmp_path) as env:
        agent = await env.agent(scheduling__debounce_seconds=0, **READ_HALL)
        await env.create(agent, "subscription", {"entity_ids": ["light.hall"]})
        await env.agents.set_enabled(agent.id, False)
        await home.set_state("light.hall", "off")

        async def dropped() -> bool:
            rows = await env.events(agent)
            return bool(rows) and rows[0]["outcome"] == "dropped"

        await eventually(dropped, 4)
        assert env.router.requests == []


async def test_deleting_an_agent_removes_its_jobs_and_subscriptions(
    home: FakeHA, tmp_path: Path
) -> None:
    async with scheduler_env(home, tmp_path) as env:
        agent = await env.agent(**READ_HALL)
        timer = await env.create(
            agent, "schedule", {"type": "timer", "delay_seconds": 600}
        )
        await env.create(agent, "subscription", {"entity_ids": ["light.hall"]})
        assert env.job(f"review:{agent.id}") is not None
        await env.agents.delete(agent.id, "delete")
        await wait_until(lambda: env.job(f"review:{agent.id}") is None)
        await wait_until(lambda: env.job(f"item:{timer}") is None)
        items = await env.ctx.db.run(
            lambda c: sched_store.list_items(c, agent_id=agent.id)
        )
        assert items == []


# ------------------------------------------------------------- AC 5


async def _restart_with(
    home: FakeHA, tmp_path: Path, clock: Clock
) -> tuple[FakeRouter, list[dict[str, Any]], list[str], dict[str, Any]]:
    """Start the app again with ``clock``. Return requests, runs and job IDs."""
    router = FakeRouter()
    async with scheduler_env(home, tmp_path, clock=clock, router=router) as env:
        agents = await env.agents.list_agents()
        runs = [r for a in agents for r in await env.runs(a)]
        scheduler = env.scheduler._scheduler
        assert scheduler is not None
        jobs = {job.id: job.next_run_time for job in scheduler.get_jobs()}
        await asyncio.sleep(0)
        return router, runs, sorted(jobs), jobs


async def _timer(
    home: FakeHA, tmp_path: Path, clock: Clock
) -> tuple[Agent, str, datetime]:
    async with scheduler_env(home, tmp_path, clock=clock) as env:
        agent = await env.agent(review__enabled=False, **READ_HALL)
        timer = await env.create(
            agent, "schedule", {"type": "timer", "delay_seconds": 120}
        )
        item = await env.scheduler.get_item(timer)
        assert item is not None
        assert item.run_at is not None
        return agent, timer, datetime.fromisoformat(item.run_at)


@pytest.mark.parametrize(
    ("late", "fires"),
    [
        (timedelta(minutes=3), True),
        (timedelta(seconds=599), True),
        (timedelta(seconds=600), False),
        (timedelta(minutes=20), False),
    ],
)
async def test_missed_timer_runs_once_if_less_than_ten_minutes_late(
    home: FakeHA, tmp_path: Path, late: timedelta, fires: bool
) -> None:
    clock = Clock()
    _, timer, run_at = await _timer(home, tmp_path, clock)
    clock.fixed = run_at + late
    router, runs, job_ids, _ = await _restart_with(home, tmp_path, clock)
    assert f"item:{timer}" not in job_ids
    if fires:
        assert [r.source for r in router.requests] == ["schedule"]
        assert router.requests[0].data["schedule"]["due_at"] == run_at.isoformat()
        assert [r["status"] for r in runs] == ["submitted"]
    else:
        assert router.requests == []
        assert [r["status"] for r in runs] == ["skipped_missed"]
        assert runs[0]["due_at"] == run_at.isoformat()
    # Both ways, the timer is gone.
    async with scheduler_env(home, tmp_path, clock=clock) as env:
        assert await env.scheduler.get_item(timer) is None


async def test_missed_cron_runs_coalesce_into_one_run(
    home: FakeHA, tmp_path: Path
) -> None:
    clock = Clock()
    async with scheduler_env(home, tmp_path, clock=clock) as env:
        agent = await env.agent(review__enabled=False, **READ_HALL)
        cron = await env.create(
            agent, "schedule", {"type": "cron", "cron": "*/5 * * * *"}
        )
        first = env.job(f"item:{cron}").next_run_time
    clock.fixed = first + timedelta(minutes=12)
    router, runs, _, jobs = await _restart_with(home, tmp_path, clock)
    assert len(router.requests) == 1
    schedule = router.requests[0].data["schedule"]
    assert schedule["missed_count"] == 3
    assert (
        schedule["due_at"]
        == (first + timedelta(minutes=10)).astimezone(UTC).isoformat()
    )
    assert runs[0]["missed_count"] == 3
    assert jobs[f"item:{cron}"] == first + timedelta(minutes=15)


async def test_a_missed_cron_run_too_late_is_skipped_and_recorded(
    home: FakeHA, tmp_path: Path
) -> None:
    clock = Clock()
    async with scheduler_env(home, tmp_path, clock=clock) as env:
        agent = await env.agent(review__enabled=False, **READ_HALL)
        cron = await env.create(
            agent, "schedule", {"type": "cron", "cron": "0 4 * * *"}
        )
        first = env.job(f"item:{cron}").next_run_time
    clock.fixed = first + timedelta(minutes=15)
    router, runs, _, jobs = await _restart_with(home, tmp_path, clock)
    assert router.requests == []
    assert [(r["status"], r["item_id"]) for r in runs] == [("skipped_missed", cron)]
    assert jobs[f"item:{cron}"] == first + timedelta(days=1)


async def test_a_router_that_is_down_is_recorded(home: FakeHA, tmp_path: Path) -> None:
    async with scheduler_env(home, tmp_path) as env:
        agent = await env.agent(**READ_HALL)
        timer = await env.create(
            agent, "schedule", {"type": "timer", "delay_seconds": 600}
        )
        env.router.down = True
        await env.scheduler.fire(f"item:{timer}")
        runs = await env.runs(agent)
        assert [(r["status"], r["result_code"]) for r in runs] == [
            ("skipped_unavailable", "RouterNotRunningError")
        ]


async def test_a_refused_invocation_is_recorded(home: FakeHA, tmp_path: Path) -> None:
    async with scheduler_env(home, tmp_path) as env:
        agent = await env.agent(**READ_HALL)
        timer = await env.create(
            agent, "schedule", {"type": "timer", "delay_seconds": 600}
        )
        env.router.refuse = "agent_off"
        await env.scheduler.fire(f"item:{timer}")

        async def refused() -> bool:
            runs = await env.runs(agent)
            return bool(runs[0]["status"] == "refused")

        await eventually(refused, 2)
        assert (await env.runs(agent))[0]["result_code"] == "agent_off"


# ------------------------------------------------------------- AC 6


async def test_review_invocations_follow_the_definition_schedule(
    home: FakeHA, tmp_path: Path
) -> None:
    clock = Clock()
    async with scheduler_env(home, tmp_path, clock=clock) as env:
        agent = await env.agent(review__cron="30 3 * * *", **READ_HALL)
        await wait_until(lambda: env.job(f"review:{agent.id}") is not None)
        due = env.job(f"review:{agent.id}").next_run_time
        assert (due.hour, due.minute) == (3, 30)
        expected = cron_trigger("30 3 * * *", UTC).get_next_fire_time(
            None, datetime.now(UTC)
        )
        assert due == expected
        # A direct fire submits a review invocation.
        await env.scheduler.fire(f"review:{agent.id}")
        request = env.router.requests[-1]
        assert request.source == "review"
        assert request.data["review"]["cron"] == "30 3 * * *"
    # The app was stopped at the due time and starts one minute later.
    clock.fixed = due + timedelta(minutes=1)
    router, runs, _, jobs = await _restart_with(home, tmp_path, clock)
    assert [r.source for r in router.requests] == ["review"]
    assert [(r["source"], r["item_id"]) for r in runs][-1] == ("review", None)
    assert jobs[f"review:{agent.id}"] == due + timedelta(days=1)


async def test_a_definition_change_moves_or_removes_the_review_job(
    home: FakeHA, tmp_path: Path
) -> None:
    async with scheduler_env(home, tmp_path) as env:
        agent = await env.agent(review__cron="0 4 * * *", **READ_HALL)
        await wait_until(lambda: env.job(f"review:{agent.id}") is not None)
        await env.update(agent, review__cron="15 6 * * 0", **READ_HALL)
        await wait_until(lambda: env.job(f"review:{agent.id}").next_run_time.hour == 6)
        due = env.job(f"review:{agent.id}").next_run_time
        assert due.strftime("%A") == "Sunday"
        await env.update(agent, review__enabled=False, **READ_HALL)
        await wait_until(lambda: env.job(f"review:{agent.id}") is None)


# ---------------------------------------------------------- redaction


async def test_no_delivered_payload_holds_an_approval_secret(
    home: FakeHA, tmp_path: Path
) -> None:
    async with scheduler_env(home, tmp_path) as env:
        admin = await env.agent(
            tools__self=True,
            tools__ha={"level": "admin"},
            scheduling__debounce_seconds=0,
        )
        await env.create(
            admin, "subscription", {"event_type": "mobile_app_notification_action"}
        )
        await env.create(admin, "subscription", {"entity_ids": ["light.hall"]})
        await home.fire_event(
            "mobile_app_notification_action",
            {
                "action": f"HAGENT_ACCEPT_{HEX}",
                "tag": f"hagent-approval-{HEX}",
                "nested": {f"HAGENT_REJECT_{HEX}": [f"see hagent-approval-{HEX}"]},
            },
        )
        await home.set_state(
            "light.hall",
            "off",
            {"note": f"x HAGENT_REJECT_{HEX} y", f"hagent-approval-{HEX}": 1},
        )
        await wait_until(lambda: len(env.router.requests) == 2, 4.0)
        for request in env.router.requests:
            assert not contains_secret(request.data)
            assert "[redacted]" in json.dumps(request.data)


async def test_call_service_events_are_never_subscribed(
    home: FakeHA, tmp_path: Path
) -> None:
    async with scheduler_env(home, tmp_path) as env:
        admin = await env.agent(tools__self=True, tools__ha={"level": "admin"})
        result = await env.call(
            admin,
            "self.create",
            {"kind": "subscription", "spec": {"event_type": "call_service"}},
        )
        assert result.error_code == "event_not_permitted"
        # A row that bypassed the tools (for example an old database) is
        # ignored too.
        await env.ctx.db.run(
            lambda c: sched_store.insert_item(
                c,
                agent_id=admin.id,
                kind="subscription",
                spec={"event_type": "call_service"},
                run_at=None,
                limit=20,
                now=datetime.now(UTC).isoformat(),
            )
        )
    home.commands.clear()
    async with scheduler_env(home, tmp_path) as env:
        types = [c.get("event_type") for c in home.commands_of("subscribe_events")]
        assert "call_service" not in types


def test_redact_replaces_keys_and_values_at_every_depth() -> None:
    data = {
        "a": f"HAGENT_ACCEPT_{HEX}",
        f"hagent-approval-{HEX}": [{"b": f"x HAGENT_REJECT_{HEX}"}],
        "c": 3,
    }
    assert contains_secret(data)
    clean = redact(data)
    assert not contains_secret(clean)
    assert clean["c"] == 3
    assert clean["a"] == "[redacted]"


# --------------------------------------------------------------- cron


@pytest.mark.parametrize(
    ("cron", "days"),
    [
        ("0 4 * * 0", {"Sunday"}),
        ("0 4 * * 7", {"Sunday"}),
        ("0 4 * * 1-5", {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday"}),
        ("0 4 * * */3", {"Sunday", "Wednesday", "Saturday"}),
    ],
)
def test_cron_day_of_week_uses_the_standard_numbers(cron: str, days: set[str]) -> None:
    trigger = cron_trigger(cron, UTC)
    start = datetime(2026, 11, 15, tzinfo=UTC)
    seen = set()
    previous = None
    current = start
    for _ in range(14):
        fire = trigger.get_next_fire_time(previous, current)
        assert fire is not None
        seen.add(fire.strftime("%A"))
        previous, current = fire, fire + timedelta(seconds=1)
    assert seen == days


def test_min_gap_finds_the_smallest_gap() -> None:
    now = datetime(2026, 11, 15, tzinfo=UTC)
    assert min_gap_seconds(cron_trigger("0,1 * * * *", UTC), now) == 60
    assert min_gap_seconds(cron_trigger("0 4 * * *", UTC), now) == 86400


async def test_rule_calls_without_a_rule_store_never_fail_the_check(
    home: FakeHA, tmp_path: Path
) -> None:
    async with scheduler_env(home, tmp_path) as env:
        agent = await env.agent(**READ_HALL)
        calls: list[tuple[str, dict[str, Any]]] = [
            ("self.set_enabled", {"kind": "rule", "item_id": "r1", "enabled": False}),
            ("self.delete", {"kind": "rule", "item_id": "r1"}),
        ]
        for tool, args in calls:
            result = await env.call(agent, tool, args)
            assert result.error_code == "not_owner"
        listed = await env.call(agent, "self.list", {"kind": "rule"})
        assert listed.error_code == "not_supported"


async def test_the_prompt_view_of_an_item_holds_no_changing_time(
    home: FakeHA, tmp_path: Path
) -> None:
    async with scheduler_env(home, tmp_path) as env:
        agent = await env.agent(**READ_HALL)
        cron = await env.create(
            agent, "schedule", {"type": "cron", "cron": "0 4 * * *", "note": "daily"}
        )
        listed = await env.call(agent, "self.list", {"kind": "schedule"})
        item = listed.data[0]
        assert item["id"] == cron
        assert item["description"] == "cron 0 4 * * *: daily"


async def test_a_timer_missed_at_runtime_is_deleted(
    home: FakeHA, tmp_path: Path
) -> None:
    async with scheduler_env(home, tmp_path) as env:
        agent = await env.agent(**READ_HALL)
        timer = await env.create(
            agent, "schedule", {"type": "timer", "delay_seconds": 600}
        )
        scheduler = env.scheduler._scheduler
        assert scheduler is not None
        due = env.job(f"item:{timer}").next_run_time
        scheduler.remove_job(f"item:{timer}")  # APScheduler drops a missed date job.
        await env.scheduler._missed_at_runtime(f"item:{timer}", due)
        assert await env.scheduler.get_item(timer) is None
        assert [r["status"] for r in await env.runs(agent)] == ["skipped_missed"]
