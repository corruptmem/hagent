"""The test invocation, ``POST api/agents/{id}/test`` (task-12, task-18 AC 5).

A test invocation runs one dry-run agent turn. The tools check the scope as
usual, but no service call reaches HA. Sample states replace the state
cache for the turn.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer
from hagent_app.agents import Agent
from hagent_app.app import AppContext
from hagent_app.config import Config
from hagent_app.http import HttpServer
from hagent_app.router import RouterComponent
from hagent_app.tools import StateView
from hagent_app.turns import TurnHistory, TurnInput, TurnResult, TurnRunner
from helpers.agents import definition
from helpers.tools import Harness, harness
from pydantic_ai.messages import ToolReturnPart
from test_turns import Models, Script, text, tool

CONTROL_KITCHEN = {
    "tools__ha__level": "control",
    "tools__ha__scope": {"areas": ["kitchen"]},
}


class RealTurns:
    def __init__(self, runner: TurnRunner, history: TurnHistory) -> None:
        self.runner = runner
        self._history = history

    @property
    def history(self) -> TurnHistory:
        return self._history

    async def run_turn(
        self, agent: Agent, turn: TurnInput, *, states: StateView | None = None
    ) -> TurnResult:
        return await self.runner.run_turn(agent, turn, states=states)


@dataclass
class Env:
    h: Harness
    chat: Script
    router: RouterComponent
    client: TestClient[Any, Any]

    async def post(self, agent_id: str, body: Any) -> tuple[int, Any]:
        resp = await self.client.post(f"/api/agents/{agent_id}/test", json=body)
        return resp.status, await resp.json()


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    async with harness(tmp_path) as h:
        chat = Script()
        history = TurnHistory(h.db)
        runner = TurnRunner(lambda: h.executor, history, Models(chat))
        router = RouterComponent(RealTurns(runner, history))
        http = HttpServer(host="127.0.0.1", port=0)
        http.add_routes(router.routes())
        app = http.make_app()
        config = Config(ha_url=h.ha.url, ha_token=h.ha.token, data_dir=tmp_path)
        async with TestClient(TestServer(app)) as client:
            # Before start, the route answers 503 not_ready.
            resp = await client.post(f"/api/agents/{'a' * 32}/test", json={})
            assert resp.status == 503
            assert (await resp.json())["error"]["code"] == "not_ready"
            await router.start(
                AppContext(config, h.db, h.client, h.states, "test-installation")
            )
            try:
                yield Env(h, chat, router, client)
            finally:
                await router.stop()
                await runner.close()


async def test_test_invocation_plans_calls_and_never_reaches_ha(env: Env) -> None:
    agent = await env.h.agent(
        **CONTROL_KITCHEN, inputs={"entities": ["sensor.lounge_temp"]}
    )
    env.chat.replies = [
        tool("ha.get_state", {"entity_id": "light.kitchen"}, "c1"),
        tool(
            "ha.call_service",
            {
                "domain": "light",
                "service": "turn_on",
                "target": {"entity_id": "light.kitchen"},
                "data": {"brightness": 100},
            },
            "c2",
        ),
        tool(
            "ha.call_service",
            {
                "domain": "light",
                "service": "turn_on",
                "target": {"entity_id": "light.bedroom"},
            },
            "c3",
        ),
        text("The kitchen light is on."),
    ]
    env.h.reset()
    status, body = await env.post(
        agent.id,
        {
            "input": {"text": "Is it dark?", "data": {"reason": "test"}},
            "states": {
                "light.kitchen": {"state": "off", "attributes": {"sample": True}},
                "sensor.lounge_temp": {"state": "19.5", "attributes": {}},
            },
        },
    )
    assert status == 200, body
    assert body["result"] == "The kitchen light is on."
    assert body["status"] == "ok"
    assert body["path"] == "agent_turn"
    assert body["tool_calls"] == 3
    assert body["tokens"] > 0
    assert body["planned_calls"] == [
        {
            "domain": "light",
            "service": "turn_on",
            "target": {"entity_id": ["light.kitchen"]},
            "data": {"brightness": 100},
            "return_response": False,
            "needs_approval": False,
        }
    ]
    [denied] = body["denied_calls"]
    assert denied["tool"] == "ha.call_service"
    assert denied["target"] == {"entity_id": "light.bedroom"}
    assert denied["reason"]
    # No service call reached HA.
    assert env.h.ha.commands_of("call_service") == []

    # The model read the sample state, not the state in HA.
    tool_returns = [
        str(part.content)
        for message in env.chat.calls[1].messages
        for part in getattr(message, "parts", [])
        if isinstance(part, ToolReturnPart) and part.tool_name == "ha__get_state"
    ]
    assert '"state": "off"' in tool_returns[0]
    # The sample states and the input entities go into the invocation part.
    prompt = env.chat.calls[0].user_prompt
    assert "19.5" in prompt
    assert "Is it dark?" in prompt

    row = await env.router.get(body["invocation_id"])
    assert row is not None
    assert row["dry_run"] is True
    assert row["source"] == "test"


async def test_test_invocation_of_a_draft_definition(env: Env) -> None:
    agent = await env.h.agent()
    draft = json.loads(agent.definition.to_json())
    draft["instructions"] = "Draft instructions."
    env.chat.default = text("draft answer")
    status, body = await env.post(agent.id, {"definition": draft})
    assert status == 200, body
    assert body["result"] == "draft answer"
    assert "Draft instructions." in str(env.chat.calls[0].info.instructions)
    row = await env.router.get(body["invocation_id"])
    assert row is not None
    assert row["request"]["caller"] == {"draft": True}
    # The draft is not saved.
    current = await env.h.agents.get(agent.id)
    assert current.version == 1
    assert current.definition.instructions == "Keep the rooms warm."


async def test_test_invocation_does_not_use_the_queue(env: Env) -> None:
    agent = await env.h.agent(concurrency={"mode": "single"})
    env.chat.default = text("ok")
    status, _ = await env.post(agent.id, {})
    assert status == 200
    # A turned-off agent can still be tested.
    await env.h.agents.set_enabled(agent.id, False)
    status, body = await env.post(agent.id, {})
    assert status == 200
    assert body["result"] == "ok"


async def test_test_invocation_errors(env: Env) -> None:
    agent = await env.h.agent()
    status, body = await env.post("f" * 32, {})
    assert (status, body["error"]["code"]) == (404, "agent_not_found")

    bad = definition(budget={"max_tool_calls": 0})
    status, body = await env.post(agent.id, {"definition": bad})
    assert (status, body["error"]["code"]) == (422, "definition_invalid")

    # The private store must stay attached, as in an edit.
    draft = json.loads(agent.definition.to_json())
    draft["knowledge"] = []
    status, body = await env.post(agent.id, {"definition": draft})
    assert (status, body["error"]["code"]) == (422, "definition_invalid")
    assert body["error"]["details"][0]["type"] == "private_store_missing"

    bad_bodies: list[Any] = [
        [],
        {"input": "text"},
        {"input": {"text": 1}},
        {"input": {"data": []}},
        {"states": {"light.kitchen": "on"}},
        {"definition": "x"},
    ]
    for bad_body in bad_bodies:
        status, body = await env.post(agent.id, bad_body)
        assert (status, body["error"]["code"]) == (400, "invalid_body"), bad_body


async def test_test_invocation_failure_reports_the_fallback(env: Env) -> None:
    agent = await env.h.agent(fallback={"mode": "no_change"})

    def boom(_call: Any) -> Any:
        msg = "model down"
        raise RuntimeError(msg)

    env.chat.default = boom
    status, body = await env.post(agent.id, {})
    assert status == 200
    assert body["status"] == "fallback"
    assert body["path"] == "fallback"
    assert body["fallback_reason"] == "turn_failed"
