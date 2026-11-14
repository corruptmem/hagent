from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from hagent.agent import Agent
from hagent.ha import HomeAssistantClient
from hagent.tools import Toolbox


def text(value: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=value)


def tool_use(id_: str, name: str, tool_input: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=tool_input)


class ScriptedAPI:
    """Returns pre-set responses and records each request."""

    def __init__(self, responses: list[SimpleNamespace]) -> None:
        self._responses = responses
        self.requests: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> SimpleNamespace:
        # Copy the history: the agent mutates the same list later.
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        return self._responses.pop(0)


def test_agent_runs_tool_and_returns_answer(ha: HomeAssistantClient) -> None:
    api = ScriptedAPI(
        [
            SimpleNamespace(
                stop_reason="tool_use",
                content=[tool_use("t1", "get_state", {"entity_id": "climate.lounge"})],
            ),
            SimpleNamespace(stop_reason="end_turn", content=[text("It is 19.5 °C.")]),
        ]
    )
    agent = Agent(api, Toolbox(ha), model="test-model")

    assert agent.ask("How warm is the lounge?") == "It is 19.5 °C."

    second = api.requests[1]
    assert second["model"] == "test-model"
    result = second["messages"][-1]["content"][0]
    assert result["type"] == "tool_result"
    assert result["tool_use_id"] == "t1"
    assert result["is_error"] is False
    assert json.loads(result["content"])["state"] == "heat"


def test_agent_reports_tool_errors_to_model(ha: HomeAssistantClient) -> None:
    api = ScriptedAPI(
        [
            SimpleNamespace(
                stop_reason="tool_use",
                content=[tool_use("t1", "get_state", {"entity_id": "light.nowhere"})],
            ),
            SimpleNamespace(stop_reason="end_turn", content=[text("Not found.")]),
        ]
    )
    Agent(api, Toolbox(ha), model="m").ask("Is the hall light on?")
    result = api.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True


def test_failed_turn_leaves_history_unchanged(ha: HomeAssistantClient) -> None:
    class FailingAPI(ScriptedAPI):
        def create(self, **kwargs: Any) -> SimpleNamespace:
            if self._responses:
                return super().create(**kwargs)
            raise RuntimeError("network down")

    api = FailingAPI(
        [
            SimpleNamespace(
                stop_reason="tool_use",
                content=[tool_use("t1", "list_entities", {})],
            )
        ]
    )
    agent = Agent(api, Toolbox(ha), model="m")
    with pytest.raises(RuntimeError):
        agent.ask("List everything")
    assert agent.history == []
