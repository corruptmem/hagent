"""The HTTP server component: route hook, middleware slot and events."""

from __future__ import annotations

from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.typedefs import Handler
from hagent_app.app import App, default_components
from hagent_app.config import Config
from hagent_app.http import ApiError, ChangeEvent, ChangeEvents, HttpServer
from helpers.fake_ha import FakeHA
from helpers.wait import wait_until


def _config(fake_ha: FakeHA, data_dir: Path) -> Config:
    return Config(ha_url=fake_ha.url, ha_token=fake_ha.token, data_dir=data_dir, port=0)


async def test_routes_middleware_and_lifecycle(fake_ha: FakeHA, tmp_path: Path) -> None:
    order: list[str] = []

    @web.middleware
    async def outer(request: web.Request, handler: Handler) -> web.StreamResponse:
        order.append("outer")
        if request.headers.get("X-Block"):
            raise ApiError(403, "forbidden", "blocked by the outer middleware")
        return await handler(request)

    async def hello(_request: web.Request) -> web.Response:
        order.append("handler")
        return web.json_response({"hello": True})

    async def fail(_request: web.Request) -> web.Response:
        raise ApiError(418, "teapot", "no coffee", {"x": 1})

    http = HttpServer(host="127.0.0.1")
    http.add_middleware(outer)
    http.add_routes([web.get("/api/hello", hello), web.get("/api/fail", fail)])
    app = App(_config(fake_ha, tmp_path), [http])
    await app.start()
    try:
        assert http.port
        base = f"http://127.0.0.1:{http.port}"
        with pytest.raises(RuntimeError):
            http.add_routes([web.get("/late", hello)])
        with pytest.raises(RuntimeError):
            http.add_middleware(outer)
        async with aiohttp.ClientSession() as session:
            async with session.get(base + "/api/hello") as resp:
                assert resp.status == 200
                assert await resp.json() == {"hello": True}
            assert order == ["outer", "handler"]
            async with session.get(
                base + "/api/hello", headers={"X-Block": "1"}
            ) as resp:
                assert resp.status == 403
                assert (await resp.json())["error"]["code"] == "forbidden"
            async with session.get(base + "/api/fail") as resp:
                assert resp.status == 418
                assert await resp.json() == {
                    "error": {
                        "code": "teapot",
                        "message": "no coffee",
                        "details": {"x": 1},
                    }
                }
            async with session.post(base + "/api/hello") as resp:
                assert resp.status == 405
                assert (await resp.json())["error"]["code"] == "method_not_allowed"
    finally:
        await app.stop()
    assert http.port is None


async def test_events_websocket_closes_on_stop(fake_ha: FakeHA, tmp_path: Path) -> None:
    http = HttpServer(host="127.0.0.1")
    app = App(_config(fake_ha, tmp_path), [http])
    await app.start()
    try:
        async with (
            aiohttp.ClientSession() as session,
            session.ws_connect(f"http://127.0.0.1:{http.port}/api/events") as ws,
        ):
            assert (await ws.receive_json(timeout=2))["action"] == "ready"
            http.events.publish("rules", "updated", "r1", {"version": 2})
            assert await ws.receive_json(timeout=2) == {
                "topic": "rules",
                "action": "updated",
                "id": "r1",
                "data": {"version": 2},
            }
            await app.stop()
            msg = await ws.receive(timeout=2)
            assert msg.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED}
    finally:
        await app.stop()


async def test_slow_event_client_is_dropped(
    fake_ha: FakeHA, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("hagent_app.http.server.EVENT_QUEUE_SIZE", 2)
    http = HttpServer(host="127.0.0.1")
    app = App(_config(fake_ha, tmp_path), [http])
    await app.start()
    try:
        async with (
            aiohttp.ClientSession() as session,
            session.ws_connect(f"http://127.0.0.1:{http.port}/api/events") as ws,
        ):
            await ws.receive_json(timeout=2)
            for i in range(10):
                http.events.publish("t", "a", str(i))
            await wait_until(lambda: not http._clients)
    finally:
        await app.stop()


def test_listener_errors_do_not_stop_others() -> None:
    events = ChangeEvents()
    seen: list[ChangeEvent] = []

    def bad(_event: ChangeEvent) -> None:
        raise RuntimeError

    events.subscribe(bad)
    unsubscribe = events.subscribe(seen.append)
    events.publish("t", "a", "1")
    unsubscribe()
    events.publish("t", "a", "2")
    assert [e.id for e in seen] == ["1"]


async def test_default_components_serve_the_api(
    fake_ha: FakeHA, tmp_path: Path
) -> None:
    components = default_components()
    assert [c.name for c in components] == [
        "settings",
        "providers",
        "ingress_guard",
        "agents",
        "approvals",
        "tools",
        "knowledge",
        "turns",
        "rules",
        "backtest",
        "router",
        "scheduler",
        "link",
        "ha_catalog",
        "ui",
        "http",
    ]
    # The default allows only the Supervisor address. The test client
    # connects from 127.0.0.1, so the test allows that address.
    config = Config(
        ha_url=fake_ha.url,
        ha_token=fake_ha.token,
        data_dir=tmp_path,
        port=0,
        options={"ingress": {"allowed_addresses": ["127.0.0.1"]}},
    )
    app = App(config, components)
    await app.start()
    try:
        http = components[-1]
        assert isinstance(http, HttpServer)
        async with (
            aiohttp.ClientSession() as session,
            session.get(f"http://127.0.0.1:{http.port}/api/agents") as resp,
        ):
            assert resp.status == 200
            assert await resp.json() == {"agents": []}
    finally:
        await app.stop()


async def test_new_agent_defaults_follow_the_settings_budget(
    fake_ha: FakeHA, tmp_path: Path
) -> None:
    """GET api/agents/defaults takes budget.daily_usd from the settings."""
    components = default_components()
    config = Config(
        ha_url=fake_ha.url,
        ha_token=fake_ha.token,
        data_dir=tmp_path,
        port=0,
        options={
            "ingress": {"allowed_addresses": ["127.0.0.1"]},
            "providers": {"refresh_openrouter_prices": False},
        },
    )
    app = App(config, components)
    await app.start()
    try:
        http = components[-1]
        assert isinstance(http, HttpServer)
        base = f"http://127.0.0.1:{http.port}/api"
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/agents/defaults") as resp:
                body = await resp.json()
                assert body["definition"]["budget"]["daily_usd"] == 0.5
            async with session.patch(
                f"{base}/settings", json={"agent_daily_budget_usd": 0.3}
            ) as resp:
                assert resp.status == 200
            async with session.get(f"{base}/agents/defaults") as resp:
                body = await resp.json()
                assert body["definition"]["budget"]["daily_usd"] == 0.3
    finally:
        await app.stop()
