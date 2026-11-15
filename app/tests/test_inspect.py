"""Tests of the inspect routes (task-19, design section 7.2)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer
from hagent_app.agents import Agent
from hagent_app.app import AppContext
from hagent_app.config import Config
from hagent_app.http import HttpServer
from hagent_app.http.events import ChangeEvent, ChangeEvents
from hagent_app.inspection import InspectComponent
from hagent_app.link.secret import load_or_create_secret
from hagent_app.providers.meter import local_now
from hagent_app.router.models import InvocationResult
from hagent_app.tools import ToolContext, ToolsComponent
from helpers.tools import Harness, harness
from helpers.wait import wait_until

PUSD = 10**12
ACTION_ID = "HAGENT_ACCEPT_" + "c" * 32
API_KEY = "sk-test-" + "k" * 40
HALL_ON = {
    "domain": "light",
    "service": "turn_on",
    "target": {"entity_id": "light.hall"},
}


@dataclass
class Env:
    h: Harness
    tools: ToolsComponent
    inspect: InspectComponent
    events: list[ChangeEvent]
    data_dir: Path

    async def sql(self, statement: str, *args: Any) -> None:
        def run(conn: sqlite3.Connection) -> None:
            conn.execute(statement, args)

        await self.h.db.run(run)

    async def invocation(  # noqa: PLR0913
        self,
        agent: Agent,
        inv_id: str,
        *,
        path: str | None = "agent_turn",
        status: str = "ok",
        source: str = "invoke",
        request: Any = None,
        result: Any = "done",
        created_at: str | None = None,
        dry_run: bool = False,
        turn_id: str | None = None,
    ) -> None:
        at = created_at or datetime.now(UTC).isoformat()
        await self.sql(
            "INSERT INTO invocations (id, agent_id, queue_key, source, source_key,"
            " dry_run, request, created_at, started_at, finished_at, status, path,"
            " result, agent_version, turn_id, cost_pusd)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            inv_id,
            agent.id,
            agent.id,
            source,
            source,
            int(dry_run),
            json.dumps(request or {"data": {}}),
            at,
            at,
            at,
            status,
            path,
            json.dumps(result),
            agent.version,
            turn_id,
            0,
        )

    async def provider_call(  # noqa: PLR0913
        self,
        agent_id: str | None,
        invocation_id: str | None,
        *,
        call_type: str = "chat",
        usd: float = 0.01,
        day: str | None = None,
        cache_read: int = 0,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        await self.sql(
            "INSERT INTO provider_calls (started_at, finished_at, budget_day,"
            " agent_id, provider, call_type, model, status, input_tokens,"
            " cache_read_tokens, output_tokens, cost_pusd, cost_source,"
            " invocation_id) VALUES (?, ?, ?, ?, 'openai', ?, 'm', 'ok', 100, ?,"
            " 10, ?, 'price_table', ?)",
            now,
            now,
            day or local_now().date().isoformat(),
            agent_id,
            call_type,
            cache_read,
            int(usd * PUSD),
            invocation_id,
        )


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    async with harness(tmp_path) as h:
        config = Config(ha_url=h.ha.url, ha_token=h.ha.token, data_dir=tmp_path)
        ctx = AppContext(config, h.db, h.client, h.states, "test-installation")
        events = ChangeEvents()
        seen: list[ChangeEvent] = []
        events.subscribe(seen.append)
        tools = ToolsComponent()
        inspect = InspectComponent(events, tools=tools)
        await tools.start(ctx)
        await inspect.start(ctx)
        await wait_until(
            lambda: tools.registry is not None and not tools.registry.stale
        )
        try:
            yield Env(h, tools, inspect, seen, tmp_path)
        finally:
            await inspect.stop()
            await tools.stop()


@pytest.fixture
async def api(env: Env) -> AsyncIterator[TestClient[Any, Any]]:
    http = HttpServer(host="127.0.0.1", port=0)
    http.add_routes(env.inspect.routes())
    async with TestClient(TestServer(http.make_app())) as client:
        yield client


async def _get(api: TestClient[Any, Any], path: str) -> tuple[int, Any]:
    resp = await api.get(path)
    return resp.status, await resp.json()


def _hex(char: str) -> str:
    return char * 32


# ---------------------------------------------------------------- invocations


async def test_the_invocation_list_filters(env: Env, api: TestClient[Any, Any]) -> None:
    one = await env.h.agent(name="One")
    two = await env.h.agent(name="Two")
    old = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    await env.invocation(one, _hex("1"), created_at=old)
    await env.invocation(one, _hex("2"), path="rule", source="schedule")
    await env.invocation(two, _hex("3"), path="fallback", status="fallback")
    await env.invocation(two, _hex("4"), dry_run=True)

    status, body = await _get(api, "/api/inspect/invocations")
    assert status == 200
    assert [i["id"] for i in body["invocations"]] == [_hex("3"), _hex("2"), _hex("1")]
    first = body["invocations"][0]
    assert (first["agent_id"], first["path"], first["status"]) == (
        two.id,
        "fallback",
        "fallback",
    )
    assert "request" not in first

    async def ids(query: str) -> list[str]:
        status, body = await _get(api, f"/api/inspect/invocations?{query}")
        assert status == 200, body
        return [i["id"] for i in body["invocations"]]

    assert await ids(f"agent_id={one.id}") == [_hex("2"), _hex("1")]
    assert await ids("path=rule") == [_hex("2")]
    assert await ids("source=schedule") == [_hex("2")]
    assert await ids("status=fallback") == [_hex("3")]
    assert await ids("limit=1") == [_hex("3")]
    assert await ids(f"before={old}") == []
    assert _hex("4") in await ids("dry_run=true")

    for bad in ("limit=0", "path=x", "status=x", "agent_id=zz", "dry_run=1"):
        status, body = await _get(api, f"/api/inspect/invocations?{bad}")
        assert (status, body["error"]["code"]) == (400, "invalid_query"), bad


async def test_the_history_search_matches_requests_results_and_turns(
    env: Env, api: TestClient[Any, Any]
) -> None:
    agent = await env.h.agent()
    await env.invocation(agent, _hex("1"), request={"data": {"room": "Kitchen"}})
    await env.invocation(agent, _hex("2"), result="Heating 21%_x")
    await env.invocation(agent, _hex("3"), turn_id="t3")
    await env.sql(
        "INSERT INTO turns (id, agent_id, agent_version, invocation_id, source,"
        " source_key, dry_run, started_at, status, input, instructions_sha256,"
        " prompt) VALUES ('t3', ?, ?, ?, 'invoke', 'invoke', 0, ?, 'ok', ?, 'x',"
        " 'p')",
        agent.id,
        agent.version,
        _hex("3"),
        datetime.now(UTC).isoformat(),
        json.dumps({"note": "window open"}),
    )

    async def ids(q: str) -> list[str]:
        _, body = await _get(api, f"/api/inspect/invocations?q={q}")
        return [i["id"] for i in body["invocations"]]

    assert await ids("kitchen") == [_hex("1")]
    assert await ids("21%25_x") == [_hex("2")]
    assert await ids("21%25_y") == []
    assert await ids("window") == [_hex("3")]


async def test_a_trace_shows_tool_calls_with_their_scope_decision(
    env: Env, api: TestClient[Any, Any]
) -> None:
    agent = await env.h.agent(
        tools__ha={"level": "control", "scope": {"entities": ["light.hall"]}}
    )
    inv = _hex("a")
    executor = env.tools.executor()
    ctx = ToolContext(agent, invocation_id=inv)
    allowed = await executor.execute(
        ctx,
        "ha.call_service",
        HALL_ON,
    )
    denied = await executor.execute(
        ctx,
        "ha.call_service",
        {"domain": "lock", "service": "unlock", "target": {"entity_id": "lock.front"}},
    )
    read = await executor.execute(ctx, "ha.get_state", {"entity_id": "light.hall"})
    assert allowed.ok and read.ok and not denied.ok
    assert allowed.audit_id is not None and denied.audit_id is not None
    await env.invocation(agent, inv, turn_id="turn1")
    now = datetime.now(UTC).isoformat()
    await env.sql(
        "INSERT INTO turns (id, agent_id, agent_version, invocation_id, source,"
        " source_key, dry_run, started_at, finished_at, status, input,"
        " instructions_sha256, prompt, result, messages, requests, tool_calls,"
        " input_tokens, cache_read_tokens, output_tokens) VALUES"
        " ('turn1', ?, ?, ?, 'invoke', 'invoke', 0, ?, ?, 'ok', ?, 'x', 'prompt',"
        " ?, ?, 2, 3, 500, 300, 40)",
        agent.id,
        agent.version,
        inv,
        now,
        now,
        json.dumps({"data": {}}),
        json.dumps("done"),
        json.dumps([{"kind": "request", "parts": [{"content": ACTION_ID}]}]),
    )
    for seq, (tool, result) in enumerate(
        [
            ("ha.call_service", allowed),
            ("ha.call_service", denied),
            ("ha.get_state", read),
        ],
        start=1,
    ):
        await env.sql(
            "INSERT INTO turn_tool_calls (turn_id, seq, created_at, tool, arguments,"
            " ok, error_code, result, result_chars, cut, audit_id)"
            " VALUES ('turn1', ?, ?, ?, '{}', ?, ?, ?, 2, 0, ?)",
            seq,
            now,
            tool,
            int(result.ok),
            result.error_code,
            json.dumps({"ok": result.ok}),
            result.audit_id,
        )
    await env.sql(
        "INSERT INTO turn_compactions (turn_id, created_at, kind, detail)"
        " VALUES ('turn1', ?, 'tool_result_cut', ?)",
        now,
        json.dumps({"seq": 3}),
    )
    await env.provider_call(agent.id, inv, usd=0.02, cache_read=300)

    status, body = await _get(api, f"/api/inspect/invocations/{inv}")
    assert status == 200
    assert body["invocation"]["request"] == {"data": {}}
    assert body["invocation"]["result"] == "done"
    (turn,) = body["turns"]
    assert turn["prompt"] == "prompt"
    assert turn["usage"]["cache_read_tokens"] == 300
    decisions = [(c["tool"], c["scope_decision"]) for c in turn["tool_calls"]]
    assert decisions == [
        ("ha.call_service", "allowed"),
        ("ha.call_service", "denied"),
        ("ha.get_state", "allowed"),
    ]
    assert turn["tool_calls"][1]["audit"]["reason_code"] is not None
    assert turn["tool_calls"][2]["audit"] is None
    assert [c["kind"] for c in turn["compactions"]] == ["tool_result_cut"]
    assert [a["decision"] for a in body["audit"]] == ["allowed", "denied"]
    assert body["cost"]["total_usd"] == pytest.approx(0.02)
    assert body["cost"]["cache_read_tokens"] == 300
    # An approval action ID never reaches the UI.
    assert ACTION_ID not in json.dumps(body)

    status, body = await _get(api, f"/api/inspect/invocations/{_hex('f')}")
    assert (status, body["error"]["code"]) == (404, "invocation_not_found")


async def test_a_rule_trace_has_the_rule_version_and_its_trial(
    env: Env, api: TestClient[Any, Any]
) -> None:
    agent = await env.h.agent()
    inv = _hex("b")
    await env.invocation(agent, inv, path="rule")
    await env.sql(
        "UPDATE invocations SET rule_id = ?, rule_version = 2 WHERE id = ?",
        _hex("e"),
        inv,
    )
    await env.sql(
        "INSERT INTO rule_trials (id, agent_id, rule_id, version, invocation_id,"
        " created_at, status, rule_result, agent_result, cost_pusd)"
        " VALUES (?, ?, ?, 2, ?, ?, 'agreed', '1', '1', 5)",
        _hex("d"),
        agent.id,
        _hex("e"),
        inv,
        datetime.now(UTC).isoformat(),
    )
    _, body = await _get(api, f"/api/inspect/invocations/{inv}")
    assert (body["invocation"]["rule_id"], body["invocation"]["rule_version"]) == (
        _hex("e"),
        2,
    )
    assert [t["status"] for t in body["trials"]] == ["agreed"]
    assert body["turns"] == []


# ---------------------------------------------------------------- audit log


async def test_the_audit_log_keeps_previous_versions_and_filters(
    env: Env, api: TestClient[Any, Any]
) -> None:
    agent = await env.h.agent(
        tools__ha={"level": "control", "scope": {"entities": ["light.hall"]}}
    )
    other = await env.h.agent(name="Other")
    ctx = ToolContext(agent, invocation_id="i1")
    executor = env.tools.executor()
    await executor.execute(
        ctx,
        "ha.call_service",
        HALL_ON,
    )
    await executor.execute(ctx, "ha.get_state", {"entity_id": "lock.front"})
    await executor.execute(
        ToolContext(other, invocation_id="i2"), "ha.get_state", {"entity_id": "x.y"}
    )
    await env.sql(
        "UPDATE tool_audit SET previous = ?, previous_note = NULL WHERE id = 1",
        json.dumps({"name": "old"}),
    )

    status, body = await _get(api, "/api/inspect/audit")
    assert status == 200
    assert [e["id"] for e in body["entries"]] == [3, 2, 1]
    assert body["entries"][2]["previous"] == {"name": "old"}
    assert body["entries"][2]["request"]["service"] == "turn_on"

    _, body = await _get(api, f"/api/inspect/audit?agent_id={agent.id}")
    assert [e["id"] for e in body["entries"]] == [2, 1]
    _, body = await _get(api, "/api/inspect/audit?decision=allowed")
    assert [e["id"] for e in body["entries"]] == [1]
    _, body = await _get(api, "/api/inspect/audit?before=3&limit=1")
    assert [e["id"] for e in body["entries"]] == [2]
    _, body = await _get(api, "/api/inspect/audit?tool=ha.get_state")
    assert [e["id"] for e in body["entries"]] == [3, 2]
    for bad in ("decision=x", "before=x", "limit=501"):
        status, body = await _get(api, f"/api/inspect/audit?{bad}")
        assert (status, body["error"]["code"]) == (400, "invalid_query")


# ---------------------------------------------------------------- permissions


async def test_permissions_resolve_scopes_to_entity_ids(
    env: Env, api: TestClient[Any, Any]
) -> None:
    agent = await env.h.agent(
        tools__ha={
            "level": "control",
            "scope": {"areas": ["kitchen"], "domains": ["weather"]},
        },
        inputs__entities=["sensor.lounge_temp"],
        approval__services=["light.turn_on"],
    )
    other = await env.h.agent(name="Other")
    await env.tools.executor().execute(
        ToolContext(agent, invocation_id="i1"),
        "ha.get_state",
        {"entity_id": "lock.front"},
    )
    await env.tools.executor().execute(
        ToolContext(other, invocation_id="i2"), "ha.get_state", {"entity_id": "x.y"}
    )

    status, body = await _get(api, f"/api/inspect/agents/{agent.id}/permissions")
    assert status == 200
    assert body["registry_stale"] is False
    ha = body["ha"]
    assert ha["level"] == "control"
    control = set(ha["control"]["entities"])
    assert {"light.kitchen", "switch.kettle", "weather.home"} <= control
    assert "light.hall" not in control
    assert "sensor.lounge_temp" in ha["read"]["entities"]
    assert "sensor.lounge_temp" not in control
    assert ha["control"]["domains"] == ["weather"]
    assert "ha.call_service" in {t["name"] for t in body["tools"]}
    assert body["approval"]["services"] == ["light.turn_on"]
    # Only the agent's own denied calls.
    assert [d["agent_id"] for d in body["denied"]] == [agent.id]
    assert body["denied"][0]["request"] == {"entity_id": "lock.front"}


async def test_admin_permissions_say_all_and_do_not_list_entities(
    env: Env, api: TestClient[Any, Any]
) -> None:
    agent = await env.h.agent(tools__ha={"level": "admin"})
    _, body = await _get(api, f"/api/inspect/agents/{agent.id}/permissions")
    assert body["ha"]["read"] == {"all": True, "entities": [], "domains": []}
    assert body["ha"]["control"]["all"] is True


async def test_permissions_of_an_unknown_agent(
    env: Env, api: TestClient[Any, Any]
) -> None:
    status, body = await _get(api, f"/api/inspect/agents/{_hex('0')}/permissions")
    assert (status, body["error"]["code"]) == (404, "agent_not_found")


# ------------------------------------------------- schedules and subscriptions


async def test_schedules_show_the_last_run_and_event(
    env: Env, api: TestClient[Any, Any]
) -> None:
    agent = await env.h.agent()
    other = await env.h.agent(name="Other")
    now = datetime.now(UTC).isoformat()
    for item_id, owner, kind in (
        ("s1", agent, "schedule"),
        ("b1", agent, "subscription"),
        ("s2", other, "schedule"),
    ):
        await env.sql(
            "INSERT INTO scheduler_items (id, agent_id, kind, spec, created_at,"
            " updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            item_id,
            owner.id,
            kind,
            json.dumps({"cron": "0 * * * *", "note": ACTION_ID}),
            now,
            now,
        )
    for at, run_status in (
        ("2026-11-15T01:00:00+00:00", "submitted"),
        (now, "refused"),
    ):
        await env.sql(
            "INSERT INTO scheduler_runs (agent_id, item_id, source, due_at, at,"
            " status) VALUES (?, 's1', 'schedule', ?, ?, ?)",
            agent.id,
            at,
            at,
            run_status,
        )
    await env.sql(
        "INSERT INTO scheduler_runs (agent_id, item_id, source, due_at, at, status)"
        " VALUES (?, NULL, 'review', ?, ?, 'submitted')",
        agent.id,
        now,
        now,
    )
    await env.sql(
        "INSERT INTO scheduler_events (subscription_id, agent_id, event_type,"
        " entity_id, origin, received_at, outcome) VALUES ('b1', ?, 'state_changed',"
        " 'light.hall', 'external', ?, 'delivered')",
        agent.id,
        now,
    )

    status, body = await _get(api, f"/api/inspect/schedules?agent_id={agent.id}")
    assert status == 200
    items = {i["id"]: i for i in body["items"]}
    assert set(items) == {"s1", "b1"}
    assert items["s1"]["last_run"]["status"] == "refused"
    assert items["b1"]["last_event"]["outcome"] == "delivered"
    assert [r["agent_id"] for r in body["reviews"]] == [agent.id]
    assert ACTION_ID not in json.dumps(body)

    _, body = await _get(api, "/api/inspect/schedules")
    assert {i["id"] for i in body["items"]} == {"s1", "b1", "s2"}


# ---------------------------------------------------------------- cost


async def test_cost_by_agent_day_and_path(env: Env, api: TestClient[Any, Any]) -> None:
    one = await env.h.agent(name="One")
    two = await env.h.agent(name="Two")
    await env.invocation(one, _hex("1"), path="agent_turn")
    await env.invocation(two, _hex("2"), path="rule")
    today = local_now().date()
    await env.provider_call(one.id, _hex("1"), usd=0.5, cache_read=200)
    await env.provider_call(two.id, _hex("2"), call_type="decision", usd=0.25)
    await env.provider_call(None, None, call_type="embedding", usd=0.125)
    old = (today - timedelta(days=40)).isoformat()
    await env.provider_call(one.id, _hex("1"), usd=9, day=old)

    status, body = await _get(api, "/api/inspect/cost")
    assert status == 200
    assert body["total"]["cost_usd"] == pytest.approx(0.875)
    assert body["total"]["embedding_cost_usd"] == pytest.approx(0.125)
    assert body["total"]["cache_read_tokens"] == 200
    assert body["total"]["calls"] == 3
    by_path = {p["path"]: p["cost_usd"] for p in body["by_path"]}
    assert by_path == pytest.approx(
        {"agent_turn": 0.5, "rule": 0.25, "embedding": 0.125}
    )
    by_agent = {a["agent_id"]: a["cost_usd"] for a in body["by_agent"]}
    assert by_agent == pytest.approx({None: 0.125, one.id: 0.5, two.id: 0.25})
    assert [d["day"] for d in body["by_day"]] == [today.isoformat()]

    _, body = await _get(api, "/api/inspect/cost?days=60")
    assert body["total"]["cost_usd"] == pytest.approx(9.875)
    assert [d["day"] for d in body["by_day"]] == [old, today.isoformat()]
    _, body = await _get(api, f"/api/inspect/cost?agent_id={two.id}")
    assert body["total"]["cost_usd"] == pytest.approx(0.25)
    for bad in ("days=0", "days=x", "days=367"):
        status, body = await _get(api, f"/api/inspect/cost?{bad}")
        assert (status, body["error"]["code"]) == (400, "invalid_query")


# ---------------------------------------------------------------- events


async def test_router_results_publish_invocation_events(env: Env) -> None:
    class Router:
        def __init__(self) -> None:
            self.results: list[Any] = []
            self.activity: list[Any] = []

        def add_result_listener(self, fn: Any) -> None:
            self.results.append(fn)

        def add_activity_listener(self, fn: Any) -> None:
            self.activity.append(fn)

    events = ChangeEvents()
    seen: list[ChangeEvent] = []
    events.subscribe(seen.append)
    router = Router()
    InspectComponent(events, router=router)  # type: ignore[arg-type]
    router.activity[0]("a" * 32, 1)
    router.results[0](InvocationResult(_hex("1"), "a" * 32, "ok", path="agent_turn"))
    assert [(e.topic, e.action, e.id) for e in seen] == [
        ("invocations", "activity", "a" * 32),
        ("invocations", "finished", _hex("1")),
    ]
    assert seen[1].data == {"agent_id": "a" * 32, "status": "ok", "path": "agent_turn"}


# ---------------------------------------------------------------- safety


async def test_no_route_returns_a_secret(env: Env, api: TestClient[Any, Any]) -> None:
    agent = await env.h.agent()
    link_secret = load_or_create_secret(env.data_dir)
    await env.sql(
        "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
        "key.openai",
        json.dumps(API_KEY),
        datetime.now(UTC).isoformat(),
    )
    await env.invocation(agent, _hex("1"))
    await env.provider_call(agent.id, _hex("1"))
    paths = [
        "/api/inspect/invocations",
        f"/api/inspect/invocations/{_hex('1')}",
        "/api/inspect/audit",
        "/api/inspect/schedules",
        "/api/inspect/cost",
        f"/api/inspect/agents/{agent.id}/permissions",
    ]
    for path in paths:
        resp = await api.get(path)
        text = await resp.text()
        assert resp.status == 200, path
        assert API_KEY not in text
        assert link_secret not in text


async def test_routes_answer_503_before_start(tmp_path: Path) -> None:
    http = HttpServer(host="127.0.0.1", port=0)
    http.add_routes(InspectComponent().routes())
    async with TestClient(TestServer(http.make_app())) as client:
        for path in (
            "/api/inspect/invocations",
            "/api/inspect/cost",
            f"/api/inspect/agents/{_hex('a')}/permissions",
        ):
            resp = await client.get(path)
            assert resp.status == 503
