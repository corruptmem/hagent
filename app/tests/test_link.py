"""Tests of the integration link (task-17).

The link runs in a real ``HttpServer`` with the real ``IngressGuard``. The
guard allows only the Supervisor address, and the test client connects from
127.0.0.1. Every guarded route therefore answers 403, and the link path is
the only path that the client reaches without Ingress.

The agent turns come from ``ScriptedTurns``. No test calls a provider or a
live HA instance.
"""

from __future__ import annotations

import asyncio
import base64
import json
import stat
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from hagent_app.agents import Agent, AgentDefinition, AgentService
from hagent_app.app import AppContext, Component, default_components
from hagent_app.config import Config
from hagent_app.http import HttpServer
from hagent_app.http.events import ChangeEvents
from hagent_app.ingress import IngressGuard
from hagent_app.link import (
    INFO_PATH,
    LINK_PATH,
    LinkComponent,
    load_or_create_secret,
    post_discovery,
    secret_matches,
)
from hagent_app.router import InvocationRequest, RouterComponent
from hagent_app.turns import Attachment, TurnInput, TurnResult
from hagent_app.turns.models import TurnRecord
from hagent_app.turns.runner import user_prompt
from helpers.agents import definition
from helpers.tools import Harness, harness
from helpers.wait import wait_until
from pydantic_ai.messages import BinaryContent
from yarl import URL

FIXTURE = Path(__file__).resolve().parents[2] / "docs" / "link-messages.json"


# ------------------------------------------------------------------ fakes


class _History:
    async def get(self, turn_id: str) -> TurnRecord | None:
        return None


@dataclass
class ScriptedTurns:
    """Agent turns. A gated turn waits for ``release``."""

    gated: bool = False
    release: asyncio.Event = field(default_factory=asyncio.Event)
    calls: list[TurnInput] = field(default_factory=list)
    cancelled: int = 0
    _history: _History = field(default_factory=_History)

    @property
    def history(self) -> _History:
        return self._history

    async def run_turn(
        self, agent: Agent, turn: TurnInput, *, states: Any = None
    ) -> TurnResult:
        self.calls.append(turn)
        try:
            if self.gated:
                await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        result: Any = turn.data.get("text", "done")
        if turn.result_schema is not None:
            result = {"answer": result}
        return TurnResult(turn_id=f"t-{turn.invocation_id}", status="ok", result=result)


@dataclass
class Env:
    h: Harness
    ctx: AppContext
    events: ChangeEvents
    agents: AgentService
    turns: ScriptedTurns
    router: RouterComponent
    link: LinkComponent
    http: HttpServer
    session: aiohttp.ClientSession
    costs: dict[str, tuple[Decimal, Decimal]]

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.http.port}"

    async def agent(self, **changes: Any) -> Agent:
        data = definition(**changes)
        return await self.agents.create(AgentDefinition.from_json(json.dumps(data)))

    async def connect(
        self, secret: str | None = None
    ) -> aiohttp.ClientWebSocketResponse:
        token = self.link.secret if secret is None else secret
        return await self.session.ws_connect(
            self.base + LINK_PATH, headers={"Authorization": f"Bearer {token}"}
        )


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    async with harness(tmp_path) as h:
        config = Config(ha_url=h.ha.url, ha_token=h.ha.token, data_dir=tmp_path, port=0)
        ctx = AppContext(config, h.db, h.client, h.states, "test-installation")
        http = HttpServer(port=0)
        events = http.events
        agents = AgentService(h.db, events)
        turns = ScriptedTurns()
        router = RouterComponent(turns)
        costs: dict[str, tuple[Decimal, Decimal]] = {}

        async def cost(agent_id: str) -> tuple[Decimal, Decimal]:
            return costs.get(agent_id, (Decimal(0), Decimal(1)))

        guard = IngressGuard(exempt_paths=LinkComponent.exempt_paths)
        http.add_middleware(guard.middleware)
        link = LinkComponent(events, router, cost=cost)
        http.add_routes(link.routes())
        components: list[Component] = [guard, router, link, http]
        for component in components:
            await component.start(ctx)
        try:
            async with aiohttp.ClientSession() as session:
                yield Env(
                    h, ctx, events, agents, turns, router, link, http, session, costs
                )
        finally:
            _BACKLOG.clear()
            for component in reversed(components):
                await component.stop()


async def receive(ws: aiohttp.ClientWebSocketResponse) -> dict[str, Any]:
    msg = await ws.receive_json(timeout=5)
    assert isinstance(msg, dict)
    return msg


# Messages that ``receive_type`` skipped, per connection. The app can send
# a ``result`` before the ``response`` of the same invocation.
_BACKLOG: dict[int, list[dict[str, Any]]] = {}


async def receive_type(
    ws: aiohttp.ClientWebSocketResponse, kind: str, **match: Any
) -> dict[str, Any]:
    """The next message of ``kind`` whose fields match ``match``."""
    backlog = _BACKLOG.setdefault(id(ws), [])

    def matches(msg: dict[str, Any]) -> bool:
        return msg["type"] == kind and all(msg.get(k) == v for k, v in match.items())

    for msg in backlog:
        if matches(msg):
            backlog.remove(msg)
            return msg
    while True:
        msg = await receive(ws)
        if matches(msg):
            return msg
        backlog.append(msg)


async def command(
    ws: aiohttp.ClientWebSocketResponse, msg_id: int, kind: str, **fields: Any
) -> dict[str, Any]:
    await ws.send_json({"id": msg_id, "type": kind, **fields})
    return await receive_type(ws, "response", id=msg_id)


# ------------------------------------------------------------ the secret


def test_secret_is_created_once_with_owner_only_mode(tmp_path: Path) -> None:
    first = load_or_create_secret(tmp_path)
    assert len(first) >= 32
    assert load_or_create_secret(tmp_path) == first
    mode = stat.S_IMODE((tmp_path / "link_secret").stat().st_mode)
    assert mode == 0o600


def test_short_secret_file_is_replaced(tmp_path: Path) -> None:
    (tmp_path / "link_secret").write_text("short", encoding="utf-8")
    assert len(load_or_create_secret(tmp_path)) >= 32


@pytest.mark.parametrize(
    ("header", "ok"),
    [
        ("Bearer right-secret-value", True),
        ("bearer right-secret-value", True),
        ("Bearer wrong-secret-value", False),
        ("Bearer ", False),
        ("Basic right-secret-value", False),
        ("right-secret-value", False),
        (None, False),
    ],
)
def test_secret_header_check(header: str | None, ok: bool) -> None:
    assert secret_matches("right-secret-value", header) is ok


# ------------------------------------------- authentication and exemption


@pytest.mark.parametrize("secret", ["wrong", "", "x" * 43])
async def test_link_refuses_a_bad_secret(env: Env, secret: str) -> None:
    with pytest.raises(aiohttp.WSServerHandshakeError) as err:
        await env.connect(secret)
    assert err.value.status == 401


async def test_link_refuses_a_missing_header(env: Env) -> None:
    async with env.session.get(env.base + LINK_PATH) as resp:
        assert resp.status == 401
        assert (await resp.json())["error"]["code"] == "unauthorized"


async def test_link_accepts_the_secret_and_sends_hello(env: Env) -> None:
    agent = await env.agent(assist={"enabled": True}, outputs=["input_boolean.x"])
    ws = await env.connect()
    hello = await receive(ws)
    assert hello["type"] == "hello"
    assert hello["protocol"] == 1
    assert hello["installation_id"] == "test-installation"
    [snap] = hello["agents"]
    assert snap["id"] == agent.id
    assert snap["assist"] is True
    assert snap["has_outputs"] is True
    assert snap["status"] == "idle"
    await ws.close()


@pytest.mark.parametrize(
    "path",
    [
        "/api/agents",
        INFO_PATH,
        "/api/link",
        "/api/link/ws2",
        "/api/linkx/ws",
        "/api/link/wsx",
        "/api/link/ws/",
        "/api/link/ws/x",
        "/api/link/ws/../agents",
        "/api/link/ws/%2e%2e/agents",
        "/api/events",
        "/",
    ],
)
async def test_exemption_covers_only_the_link_path(env: Env, path: str) -> None:
    """From a non-Ingress address, every other path gets the guard's 403.

    The URL is sent as written (``encoded=True``), so that the client does
    not remove the dot segments first.
    """
    headers = {"Authorization": f"Bearer {env.link.secret}"}
    url = URL(env.base + path, encoded=True)
    async with env.session.get(url, headers=headers) as resp:
        assert resp.status == 403
        assert (await resp.json())["error"]["code"] == "forbidden"


def test_default_components_exempt_only_the_link_path() -> None:
    components = default_components()
    guard = next(c for c in components if isinstance(c, IngressGuard))
    assert guard._exempt == ()
    assert guard._exempt_exact == {LINK_PATH}
    assert guard._exempt_path(LINK_PATH)
    assert not guard._exempt_path(LINK_PATH + "/x")
    assert not guard._exempt_path(INFO_PATH)
    assert not guard._exempt_path("/api/link")
    assert not guard._exempt_path("/api/linkx/ws")


async def test_info_route_is_served_through_ingress(tmp_path: Path) -> None:
    """With the client address allowed, the guarded route gives the secret."""
    async with harness(tmp_path) as h:
        config = Config(
            ha_url=h.ha.url,
            ha_token=h.ha.token,
            data_dir=tmp_path,
            port=0,
            options={"ingress": {"allowed_addresses": ["127.0.0.1"]}},
        )
        ctx = AppContext(config, h.db, h.client, h.states, "i")
        events = ChangeEvents()
        router = RouterComponent(ScriptedTurns())
        http = HttpServer(port=0)
        guard = IngressGuard(exempt_paths=LinkComponent.exempt_paths)
        http.add_middleware(guard.middleware)
        link = LinkComponent(events, router)
        http.add_routes(link.routes())
        components: list[Component] = [guard, router, link, http]
        for c in components:
            await c.start(ctx)
        try:
            async with (
                aiohttp.ClientSession() as session,
                session.get(f"http://127.0.0.1:{http.port}{INFO_PATH}") as resp,
            ):
                assert resp.status == 200
                body = await resp.json()
            assert body["secret"] == link.secret
            assert body["path"] == LINK_PATH
            assert body["protocol"] == 1
        finally:
            for c in reversed(components):
                await c.stop()


# ----------------------------------------------------------------- invoke


async def test_invoke_waits_and_returns_the_response_data(env: Env) -> None:
    agent = await env.agent()
    ws = await env.connect()
    await receive_type(ws, "hello")
    resp = await command(
        ws, 1, "invoke", agent_id=agent.id, data={"reason": "test"}, text="hello"
    )
    assert resp["success"] is True
    data = resp["data"]
    assert data["status"] == "ok"
    assert data["result"] == "hello"
    assert data["path"] == "agent_turn"
    assert data["writes"] == []
    assert "cost_usd" in data
    assert env.turns.calls[0].data == {"reason": "test", "text": "hello"}
    result = await receive_type(ws, "result")
    assert result["delivered"] is True
    assert result["result"]["agent_id"] == agent.id


async def test_invoke_without_wait_returns_at_once_and_sends_the_result(
    env: Env,
) -> None:
    agent = await env.agent()
    env.turns.gated = True
    ws = await env.connect()
    await receive_type(ws, "hello")
    resp = await command(ws, 1, "invoke", agent_id=agent.id, wait=False)
    assert resp["data"]["status"] == "queued"
    invocation_id = resp["data"]["invocation_id"]
    env.turns.release.set()
    result = await receive_type(ws, "result")
    assert result["delivered"] is False
    assert result["result"]["invocation_id"] == invocation_id
    assert result["result"]["status"] == "ok"


async def test_timeout_continue_finishes_in_the_background(env: Env) -> None:
    agent = await env.agent()
    env.turns.gated = True
    ws = await env.connect()
    await receive_type(ws, "hello")
    resp = await command(
        ws, 1, "invoke", agent_id=agent.id, timeout=0.2, on_timeout="continue"
    )
    assert resp["success"] is True
    assert resp["data"]["status"] == "running"
    assert resp["data"]["timed_out"] is True
    env.turns.release.set()
    result = await receive_type(ws, "result")
    assert result["delivered"] is False
    assert result["result"]["status"] == "ok"


async def test_timeout_cancel_stops_the_invocation(env: Env) -> None:
    agent = await env.agent()
    env.turns.gated = True
    ws = await env.connect()
    await receive_type(ws, "hello")
    resp = await command(
        ws, 1, "invoke", agent_id=agent.id, timeout=0.2, on_timeout="cancel"
    )
    assert resp["success"] is False
    assert resp["error"]["code"] == "timeout"
    result = await receive_type(ws, "result")
    assert result["result"]["status"] == "cancelled"
    assert result["delivered"] is True, "the caller already got the error"
    await wait_until(lambda: env.turns.cancelled == 1)


@pytest.mark.parametrize(("outputs", "expected"), [([], "running"), (None, "error")])
async def test_default_on_timeout_depends_on_outputs(
    env: Env, outputs: list[str] | None, expected: str
) -> None:
    changes: dict[str, Any] = {}
    if outputs is None:
        changes["outputs"] = ["input_boolean.x"]
    agent = await env.agent(**changes)
    env.turns.gated = True
    ws = await env.connect()
    await receive_type(ws, "hello")
    resp = await command(ws, 1, "invoke", agent_id=agent.id, timeout=0.2)
    if expected == "running":
        assert resp["data"]["status"] == "running"
    else:
        assert resp["error"]["code"] == "timeout"
    env.turns.release.set()


async def test_invoke_errors_pass_the_router_code(env: Env) -> None:
    agent = await env.agent()
    await env.agents.set_enabled(agent.id, False)
    ws = await env.connect()
    await receive_type(ws, "hello")
    resp = await command(ws, 1, "invoke", agent_id=agent.id)
    assert resp["success"] is True
    assert resp["data"]["status"] == "error"
    assert resp["data"]["error"]["code"] == "agent_off"
    missing = await command(ws, 2, "invoke", agent_id="nope")
    assert missing["data"]["error"]["code"] == "agent_not_found"


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"agent_id": "a", "source": "schedule"},
        {"agent_id": "a", "wait": "yes"},
        {"agent_id": "a", "timeout": 0},
        {"agent_id": "a", "timeout": True},
        {"agent_id": "a", "on_timeout": "later"},
        {"agent_id": "a", "data": [1]},
        {"agent_id": "a", "source": "assist"},
        {"agent_id": "a", "attachments": [{"name": "x", "mime_type": "image/png"}]},
        {
            "agent_id": "a",
            "attachments": [
                {"name": "x.exe", "mime_type": "application/x-dosexec", "data": ""}
            ],
        },
    ],
)
async def test_invalid_invoke_is_refused(env: Env, fields: dict[str, Any]) -> None:
    ws = await env.connect()
    await receive_type(ws, "hello")
    resp = await command(ws, 1, "invoke", **fields)
    assert resp["success"] is False
    assert resp["error"]["code"] == "invalid_message"


async def test_unknown_and_malformed_commands(env: Env) -> None:
    ws = await env.connect()
    await receive_type(ws, "hello")
    resp = await command(ws, 1, "explode")
    assert resp["error"]["code"] == "unknown_command"
    await ws.send_str("not json")
    bad = await receive_type(ws, "response")
    assert bad["id"] is None
    assert bad["error"]["code"] == "invalid_message"
    assert (await command(ws, 2, "ping"))["success"] is True


async def test_cancel_command(env: Env) -> None:
    agent = await env.agent()
    env.turns.gated = True
    ws = await env.connect()
    await receive_type(ws, "hello")
    resp = await command(ws, 1, "invoke", agent_id=agent.id, wait=False)
    invocation_id = resp["data"]["invocation_id"]
    await wait_until(lambda: len(env.turns.calls) == 1)
    assert (await command(ws, 2, "cancel", invocation_id=invocation_id))["success"]
    result = await receive_type(ws, "result")
    assert result["result"]["status"] == "cancelled"
    again = await command(ws, 3, "cancel", invocation_id=invocation_id)
    assert again["error"]["code"] == "not_found"


async def test_ai_task_structure_and_attachments_reach_the_turn(env: Env) -> None:
    agent = await env.agent()
    ws = await env.connect()
    await receive_type(ws, "hello")
    png = b"\x89PNG\r\n\x1a\nrest"
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}}
    resp = await command(
        ws,
        1,
        "invoke",
        agent_id=agent.id,
        source="ai_task",
        text="describe",
        data={"task_name": "look"},
        result_schema=schema,
        attachments=[
            {
                "name": "cam.png",
                "mime_type": "image/png",
                "data": base64.b64encode(png).decode(),
            }
        ],
    )
    assert resp["data"]["result"] == {"answer": "describe"}
    turn = env.turns.calls[0]
    assert turn.source == "ai_task"
    assert turn.result_schema == schema
    assert turn.attachments == (Attachment("cam.png", "image/png", png),)
    assert turn.data["attachments"] == [
        {"name": "cam.png", "mime_type": "image/png", "size": len(png)}
    ]


async def test_assist_invoke_uses_the_conversation(env: Env) -> None:
    agent = await env.agent(assist={"enabled": True})
    ws = await env.connect()
    await receive_type(ws, "hello")
    resp = await command(
        ws,
        1,
        "invoke",
        agent_id=agent.id,
        source="assist",
        text="hi",
        conversation_id="c1",
    )
    assert resp["data"]["result"] == "hi"
    assert env.turns.calls[0].conversation_id == "c1"


# ---------------------------------------------------------- set_enabled


async def test_set_enabled_turns_the_agent_off_and_sends_the_snapshot(
    env: Env,
) -> None:
    agent = await env.agent()
    ws = await env.connect()
    await receive_type(ws, "hello")
    resp = await command(ws, 1, "set_enabled", agent_id=agent.id, enabled=False)
    assert resp["data"] == {"enabled": False}
    msg = await receive_type(ws, "agent")
    assert msg["agent"]["enabled"] is False
    assert msg["agent"]["status"] == "off"
    missing = await command(ws, 2, "set_enabled", agent_id="nope", enabled=True)
    assert missing["error"]["code"] == "agent_not_found"


# ------------------------------------------------------ agent snapshots


async def test_agent_changes_reach_the_integration(env: Env) -> None:
    ws = await env.connect()
    assert (await receive_type(ws, "hello"))["agents"] == []
    agent = await env.agent()
    created = await receive_type(ws, "agent")
    assert created["agent"]["id"] == agent.id
    assert created["agent"]["assist"] is False

    data = agent.definition.to_data()
    data["assist"] = {"enabled": True}
    await env.agents.update(agent.id, AgentDefinition.from_json(json.dumps(data)))
    updated = await receive_type(ws, "agent")
    assert updated["agent"]["assist"] is True
    assert updated["agent"]["version"] == 2

    await env.agents.delete(agent.id, "delete")
    removed = await receive_type(ws, "agent_removed")
    assert removed["agent_id"] == agent.id


async def test_reconnect_gets_a_full_snapshot(env: Env) -> None:
    first = await env.agent(name="First")
    ws = await env.connect()
    await receive_type(ws, "hello")
    await ws.close()
    second = await env.agent(name="Second")
    await env.agents.delete(first.id, "delete")
    ws = await env.connect()
    hello = await receive_type(ws, "hello")
    assert [a["id"] for a in hello["agents"]] == [second.id]


async def test_status_running_budget_and_last_result(env: Env) -> None:
    agent = await env.agent()
    env.turns.gated = True
    ws = await env.connect()
    await receive_type(ws, "hello")
    await command(ws, 1, "invoke", agent_id=agent.id, wait=False, text="r1")
    running = await receive_type(ws, "agent")
    while running["agent"]["status"] != "running":
        running = await receive_type(ws, "agent")
    assert running["agent"]["running"] == 1
    env.costs[agent.id] = (Decimal("0.5"), Decimal(0))
    env.turns.release.set()
    msg = await receive_type(ws, "agent")
    while msg["agent"]["running"]:
        msg = await receive_type(ws, "agent")
    assert msg["agent"]["status"] == "budget_spent"
    assert msg["agent"]["cost_today_usd"] == 0.5
    assert msg["agent"]["last_result"]["result"] == "r1"
    assert msg["agent"]["last_result"]["status"] == "ok"
    # A new connection loads the last result from the database.
    env.link._last.clear()
    ws2 = await env.connect()
    hello = await receive_type(ws2, "hello")
    assert hello["agents"][0]["last_result"]["result"] == "r1"


async def test_activity_hook_counts_running_invocations(env: Env) -> None:
    agent = await env.agent(concurrency={"mode": "parallel", "queue_size": 3})
    seen: list[tuple[str, int]] = []
    env.router.add_activity_listener(lambda a, n: seen.append((a, n)))
    env.turns.gated = True
    handles = [
        await env.router.submit(InvocationRequest(agent.id, "invoke")) for _ in range(2)
    ]
    await wait_until(lambda: env.router.running_count(agent.id) == 2)
    env.turns.release.set()
    for handle in handles:
        await handle.result()
    await wait_until(lambda: seen[-1] == (agent.id, 0))
    assert (agent.id, 2) in seen


# --------------------------------------------------- shared message fixture


def _shape(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _shape(v) for k, v in value.items()}
    return type(value).__name__


async def test_app_messages_match_the_shared_fixture(env: Env) -> None:
    """The integration tests replay ``docs/link-messages.json``. The app must
    send messages with the same keys."""
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    agent = await env.agent(name="Heating")
    ws = await env.connect()
    hello = await receive_type(ws, "hello")
    assert set(hello) == set(fixture["hello"])
    assert set(hello["agents"][0]) == set(fixture["hello"]["agents"][0])
    resp = await command(ws, 1, "invoke", agent_id=agent.id, text="ok")
    assert set(resp) == set(fixture["invoke_response"])
    assert set(resp["data"]) == set(fixture["invoke_response"]["data"])
    result = await receive_type(ws, "result")
    assert set(result) == set(fixture["result"])
    assert set(result["result"]) == set(fixture["result"]["result"])
    snap = await receive_type(ws, "agent")
    while snap["agent"]["last_result"] is None:
        snap = await receive_type(ws, "agent")
    assert set(snap["agent"]) == set(fixture["agent"]["agent"])
    assert set(snap["agent"]["last_result"]) == set(
        fixture["agent"]["agent"]["last_result"]
    )
    error = await command(ws, 2, "invoke", agent_id=agent.id, timeout=-1)
    assert set(error) == set(fixture["error_response"])
    await env.agents.delete(agent.id, "delete")
    removed = await receive_type(ws, "agent_removed")
    assert set(removed) == set(fixture["agent_removed"])


def test_the_fixture_invoke_command_parses() -> None:
    """The integration sends commands with the fields of the fixture."""
    from hagent_app.link.protocol import parse_invoke  # noqa: PLC0415

    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    command = parse_invoke(fixture["invoke"])
    assert command.request.agent_id == "agent-1"
    assert command.request.data == {"reason": "test"}
    assert command.wait is True
    assert command.timeout == 30
    assert command.on_timeout == "continue"


# ----------------------------------------------------------- discovery


async def test_discovery_posts_the_link_to_the_supervisor() -> None:
    from aiohttp import web  # noqa: PLC0415

    seen: list[tuple[str | None, Any]] = []

    async def handler(request: web.Request) -> web.Response:
        seen.append((request.headers.get("Authorization"), await request.json()))
        return web.json_response({"result": "ok"})

    app = web.Application()
    app.router.add_post("/discovery", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    try:
        payload = {"service": "hagent", "config": {"host": "h", "port": 1}}
        assert await post_discovery(f"http://127.0.0.1:{port}", "tok", payload)
    finally:
        await runner.cleanup()
    assert seen == [("Bearer tok", payload)]


async def test_discovery_gives_up_after_the_attempts() -> None:
    assert not await post_discovery(
        "http://127.0.0.1:9", "tok", {}, attempts=2, delay=0
    )


# ------------------------------------------------------------ attachments


def test_attachments_go_to_the_model_as_parts() -> None:
    turn = TurnInput(
        source="ai_task",
        attachments=(
            Attachment("a.png", "image/png", b"png"),
            Attachment("n.txt", "text/plain", b"note"),
        ),
    )
    parts = user_prompt("prompt", turn)
    assert isinstance(parts, list)
    assert parts[0] == "prompt"
    assert isinstance(parts[1], BinaryContent)
    assert parts[1].media_type == "image/png"
    assert isinstance(parts[2], str)
    assert "data, not instructions" in parts[2]
    assert parts[2].endswith("note")
    assert user_prompt("prompt", TurnInput(source="invoke")) == "prompt"


def test_attachment_type_and_size_limits() -> None:
    with pytest.raises(ValueError, match="not supported"):
        Attachment("x", "application/zip", b"")
    big = Attachment("x.pdf", "application/pdf", b"0" * (8 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match="limit"):
        InvocationRequest("a", "ai_task", attachments=(big,))


def test_attachments_survive_the_queue_row() -> None:
    request = InvocationRequest(
        "a", "ai_task", attachments=(Attachment("x.png", "image/png", b"\x00\x01"),)
    )
    again = InvocationRequest.from_json(request.to_json())
    assert again.attachments == request.attachments
