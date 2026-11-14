"""The agents management API, end to end through the HTTP server."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer
from hagent_app.agents import AgentsComponent
from hagent_app.app import App, AppContext
from hagent_app.config import Config
from hagent_app.http import ChangeEvent, HttpServer
from helpers.agents import definition
from helpers.fake_ha import FakeHA


@dataclass
class Api:
    session: aiohttp.ClientSession
    base: str
    ctx: AppContext
    events: list[ChangeEvent]

    async def call(
        self, method: str, path: str, body: Any = None, **params: str
    ) -> tuple[int, Any]:
        async with self.session.request(
            method, self.base + path, json=body, params=params or None
        ) as resp:
            return resp.status, await resp.json()

    async def create(self, **changes: Any) -> dict[str, Any]:
        status, data = await self.call(
            "POST", "/api/agents", {"definition": definition(**changes)}
        )
        assert status == 201, data
        return data  # type: ignore[no-any-return]

    async def put(self, agent: dict[str, Any], **changes: Any) -> tuple[int, Any]:
        new = json.loads(json.dumps(agent["definition"]))
        for key, value in changes.items():
            target = new
            *parents, last = key.split("__")
            for part in parents:
                target = target[part]
            target[last] = value
        return await self.call(
            "PUT",
            f"/api/agents/{agent['id']}",
            {"definition": new, "base_version": agent["version"]},
        )

    async def stores(self) -> list[dict[str, Any]]:
        def read(conn: Any) -> list[dict[str, Any]]:
            rows = conn.execute("SELECT * FROM knowledge_stores ORDER BY name")
            return [dict(r) for r in rows]

        return await self.ctx.db.run(read)


@pytest.fixture
async def api(fake_ha: FakeHA, tmp_path: Path) -> AsyncIterator[Api]:
    config = Config(ha_url=fake_ha.url, ha_token=fake_ha.token, data_dir=tmp_path)
    http = HttpServer(host="127.0.0.1", port=0)
    agents = AgentsComponent(http.events)
    http.add_routes(agents.routes())
    events: list[ChangeEvent] = []
    http.events.subscribe(events.append)
    app = App(config, [agents, http])
    ctx = await app.start()
    try:
        async with aiohttp.ClientSession() as session:
            yield Api(session, f"http://127.0.0.1:{http.port}", ctx, events)
    finally:
        await app.stop()


# ------------------------------------------------------------- create


async def test_create_gets_private_store_and_version_1(api: Api) -> None:
    agent = await api.create()
    assert agent["version"] == 1
    assert agent["enabled"] is True
    store = agent["private_store"]
    assert store["name"] == "heating_private"
    assert agent["definition"]["knowledge"] == [
        {"store_id": store["id"], "access": "read_write", "inject_main": True}
    ]
    rows = await api.stores()
    assert rows[0]["owner_agent_id"] == agent["id"]
    assert rows[0]["private"] == 1
    assert rows[0]["description"]
    assert api.events[-1].action == "created"
    assert api.events[-1].id == agent["id"]


async def test_create_rejects_invalid_definition(api: Api) -> None:
    status, data = await api.call(
        "POST",
        "/api/agents",
        {"definition": definition(concurrency__mode="parallel", outputs=["light.a"])},
    )
    assert status == 422
    assert data["error"]["code"] == "definition_invalid"
    assert data["error"]["details"][0]["msg"]
    assert await api.stores() == []


async def test_create_rejects_unknown_store(api: Api) -> None:
    status, data = await api.call(
        "POST",
        "/api/agents",
        {"definition": definition(knowledge=[{"store_id": "b" * 32}])},
    )
    assert status == 422
    assert data["error"]["details"][0]["type"] == "store_not_found"
    assert await api.stores() == []


async def test_bad_bodies(api: Api) -> None:
    async with api.session.post(api.base + "/api/agents", data=b"{nope") as resp:
        assert resp.status == 400
        assert (await resp.json())["error"]["code"] == "invalid_json"
    assert (await api.call("POST", "/api/agents", [1]))[0] == 400
    assert (await api.call("POST", "/api/agents", {"x": 1}))[0] == 400


async def test_names_are_unique(api: Api) -> None:
    await api.create()
    status, data = await api.call(
        "POST", "/api/agents", {"definition": definition(name="heating")}
    )
    assert status == 409
    assert data["error"]["code"] == "name_conflict"


async def test_unknown_agent_and_route(api: Api) -> None:
    status, data = await api.call("GET", f"/api/agents/{'0' * 32}")
    assert status == 404
    assert data["error"]["code"] == "agent_not_found"
    status, data = await api.call("GET", "/api/nothing")
    assert status == 404
    assert data["error"]["code"] == "not_found"


async def test_list_schema_and_defaults(api: Api) -> None:
    await api.create(name="B agent")
    await api.create(name="a agent")
    status, data = await api.call("GET", "/api/agents")
    assert status == 200
    assert [a["name"] for a in data["agents"]] == ["a agent", "B agent"]
    assert "definition" not in data["agents"][0]
    status, schema = await api.call("GET", "/api/agents/schema")
    assert status == 200
    assert "budget" in schema["properties"]
    status, defaults = await api.call("GET", "/api/agents/defaults")
    assert defaults["definition"]["budget"]["daily_usd"] == 0.5


# ----------------------------------------------------- edit, versions


async def test_edit_creates_versions(api: Api) -> None:
    agent = await api.create()
    status, v2 = await api.put(agent, budget__daily_usd=1.25)
    assert status == 200
    assert v2["version"] == 2
    status, v3 = await api.put(v2, name="Heating 2")
    assert v3["version"] == 3
    assert v3["name"] == "Heating 2"

    status, data = await api.call("GET", f"/api/agents/{agent['id']}/versions")
    assert [v["version"] for v in data["versions"]] == [3, 2, 1]
    assert [v["change"] for v in data["versions"]] == ["edit", "edit", "create"]

    status, old = await api.call("GET", f"/api/agents/{agent['id']}/versions/1")
    assert status == 200
    assert old["definition"]["budget"]["daily_usd"] == 0.5
    status, data = await api.call("GET", f"/api/agents/{agent['id']}/versions/9")
    assert status == 404
    assert data["error"]["code"] == "version_not_found"


async def test_unchanged_edit_adds_no_version(api: Api) -> None:
    agent = await api.create()
    count = len(api.events)
    status, same = await api.put(agent)
    assert status == 200
    assert same["version"] == 1
    assert len(api.events) == count


async def test_edit_checks_base_version(api: Api) -> None:
    agent = await api.create()
    _, v2 = await api.put(agent, budget__daily_usd=2)
    status, data = await api.put(agent, budget__daily_usd=3)
    assert status == 409
    assert data["error"]["code"] == "version_conflict"
    assert data["error"]["details"] == {"current_version": v2["version"]}


async def test_edit_validates(api: Api) -> None:
    agent = await api.create()
    status, data = await api.put(agent, tools__ha={"level": "read"})
    assert status == 422
    status, data = await api.put(agent, knowledge=[])
    assert status == 422
    assert data["error"]["details"][0]["type"] == "private_store_missing"
    attachment = dict(agent["definition"]["knowledge"][0], access="read")
    status, data = await api.put(agent, knowledge=[attachment])
    assert status == 422
    assert data["error"]["details"][0]["type"] == "private_store_access"
    other = await api.create(name="Other")
    status, data = await api.put(agent, name="other")
    assert status == 409
    assert other["version"] == 1


async def test_diff_and_restore(api: Api) -> None:
    agent = await api.create()
    _, v2 = await api.put(agent, budget__daily_usd=2, outputs=["climate.x"])
    status, data = await api.call(
        "GET", f"/api/agents/{agent['id']}/diff", **{"from": "1", "to": "2"}
    )
    assert status == 200
    assert data["changes"] == [
        {"path": "budget.daily_usd", "old": 0.5, "new": 2},
        {"path": "outputs", "old": [], "new": ["climate.x"]},
    ]
    status, _ = await api.call("GET", f"/api/agents/{agent['id']}/diff", to="2")
    assert status == 400

    status, v3 = await api.call(
        "POST", f"/api/agents/{agent['id']}/versions/1/restore", {"base_version": 2}
    )
    assert status == 200
    assert v3["version"] == 3
    assert v3["definition"] == agent["definition"]
    _, versions = await api.call("GET", f"/api/agents/{agent['id']}/versions")
    assert versions["versions"][0]["change"] == "restore"
    assert versions["versions"][0]["source_version"] == 1
    # The restore keeps every version: version 2 is still there.
    _, old = await api.call("GET", f"/api/agents/{agent['id']}/versions/2")
    assert old["definition"] == v2["definition"]

    status, data = await api.call(
        "POST", f"/api/agents/{agent['id']}/versions/2/restore", {"base_version": 1}
    )
    assert status == 409


async def test_restore_revalidates_stores(api: Api) -> None:
    shared = await api.create(name="Shared")
    shared_store = shared["private_store"]["id"]
    agent = await api.create()
    attachments = [*agent["definition"]["knowledge"], {"store_id": shared_store}]
    _, v2 = await api.put(agent, knowledge=attachments)
    _, v3 = await api.put(v2, knowledge=agent["definition"]["knowledge"])
    status, _ = await api.call(
        "DELETE", f"/api/agents/{shared['id']}", private_store="delete"
    )
    assert status == 200
    status, data = await api.call(
        "POST", f"/api/agents/{agent['id']}/versions/2/restore"
    )
    assert status == 422
    assert data["error"]["details"][0]["type"] == "store_not_found"
    assert v3["version"] == 3


# ------------------------------------------------- copy, enable, delete


async def test_copy_gets_its_own_private_store(api: Api) -> None:
    shared = await api.create(name="Shared")
    agent = await api.create()
    attachments = [
        *agent["definition"]["knowledge"],
        {"store_id": shared["private_store"]["id"], "access": "read"},
    ]
    _, agent = await api.put(agent, knowledge=attachments)
    status, copy = await api.call("POST", f"/api/agents/{agent['id']}/copy")
    assert status == 201
    assert copy["name"] == "Heating (copy)"
    assert copy["version"] == 1
    assert copy["private_store"]["id"] != agent["private_store"]["id"]
    stores = [k["store_id"] for k in copy["definition"]["knowledge"]]
    assert stores == [copy["private_store"]["id"], shared["private_store"]["id"]]
    _, versions = await api.call("GET", f"/api/agents/{copy['id']}/versions")
    first = versions["versions"][0]
    assert first["change"] == "copy"
    assert first["source_agent_id"] == agent["id"]
    assert first["source_version"] == agent["version"]

    status, named = await api.call(
        "POST", f"/api/agents/{agent['id']}/copy", {"name": "Hot water"}
    )
    assert status == 201
    assert named["name"] == "Hot water"
    status, again = await api.call("POST", f"/api/agents/{agent['id']}/copy")
    assert again["name"] == "Heating (copy) 2"


async def test_turn_off_adds_no_version(api: Api) -> None:
    agent = await api.create()
    status, off = await api.call(
        "PUT", f"/api/agents/{agent['id']}/enabled", {"enabled": False}
    )
    assert status == 200
    assert off["enabled"] is False
    assert off["version"] == 1
    assert api.events[-1].data["enabled"] is False
    status, _ = await api.call(
        "PUT", f"/api/agents/{agent['id']}/enabled", {"enabled": "no"}
    )
    assert status == 400


async def test_delete_asks_about_private_store(api: Api) -> None:
    agent = await api.create()
    status, data = await api.call("DELETE", f"/api/agents/{agent['id']}")
    assert status == 400
    assert data["error"]["code"] == "private_store_choice_required"
    assert data["error"]["details"] == {"choices": ["keep", "delete"]}
    status, data = await api.call(
        "DELETE", f"/api/agents/{agent['id']}", private_store="maybe"
    )
    assert status == 400
    assert (await api.call("GET", f"/api/agents/{agent['id']}"))[0] == 200


async def test_delete_and_keep_store(api: Api) -> None:
    agent = await api.create()
    status, data = await api.call(
        "DELETE", f"/api/agents/{agent['id']}", private_store="keep"
    )
    assert status == 200
    assert data == {"deleted": True, "private_store": "keep"}
    assert (await api.call("GET", f"/api/agents/{agent['id']}"))[0] == 404
    [store] = await api.stores()
    assert store["id"] == agent["private_store"]["id"]
    assert store["private"] == 0
    assert store["owner_agent_id"] is None
    assert api.events[-1].action == "deleted"
    # The name is free again, and the new agent gets a new store name.
    again = await api.create()
    assert again["private_store"]["name"] == "heating_private_2"
    # History can still read the versions of the deleted agent.
    old = await api.ctx.db.run(
        lambda c: c.execute(
            "SELECT count(*) FROM agent_versions WHERE agent_id = ?", (agent["id"],)
        ).fetchone()[0]
    )
    assert old == 1


async def test_delete_and_delete_store(api: Api) -> None:
    agent = await api.create()
    status, _ = await api.call(
        "DELETE", f"/api/agents/{agent['id']}", private_store="delete"
    )
    assert status == 200
    assert await api.stores() == []


async def test_delete_store_that_others_attach(api: Api) -> None:
    owner = await api.create(name="Owner")
    user = await api.create(name="User")
    attachments = [
        *user["definition"]["knowledge"],
        {"store_id": owner["private_store"]["id"]},
    ]
    status, _ = await api.put(user, knowledge=attachments)
    assert status == 200
    status, data = await api.call(
        "DELETE", f"/api/agents/{owner['id']}", private_store="delete"
    )
    assert status == 409
    assert data["error"]["code"] == "private_store_in_use"
    assert data["error"]["details"] == {"agent_ids": [user["id"]]}
    assert (await api.call("GET", f"/api/agents/{owner['id']}"))[0] == 200
    status, _ = await api.call(
        "DELETE", f"/api/agents/{owner['id']}", private_store="keep"
    )
    assert status == 200


# --------------------------------------------------------- WebSocket


async def test_events_websocket(api: Api) -> None:
    async with api.session.ws_connect(api.base + "/api/events") as ws:
        ready = await ws.receive_json(timeout=2)
        assert ready["action"] == "ready"
        agent = await api.create()
        event = await ws.receive_json(timeout=2)
        assert event == {
            "topic": "agents",
            "action": "created",
            "id": agent["id"],
            "data": {"version": 1, "enabled": True},
        }
        await api.put(agent, budget__daily_usd=3)
        event = await ws.receive_json(timeout=2)
        assert event["action"] == "updated"
        assert event["data"]["version"] == 2


async def test_api_returns_503_before_start() -> None:
    agents = AgentsComponent(HttpServer().events)
    server = HttpServer()
    server.add_routes(agents.routes())
    async with TestClient(TestServer(server.make_app())) as client:
        resp = await client.get("/api/agents")
        assert resp.status == 503
        assert (await resp.json())["error"]["code"] == "not_ready"
