from __future__ import annotations

import json
from typing import Any

from hagent.ha import HomeAssistantClient
from hagent.tools import Toolbox

from .conftest import FakeHA


def tool_names(toolbox: Toolbox) -> set[str]:
    return {tool["name"] for tool in toolbox.definitions}


def test_read_only_by_default(ha: HomeAssistantClient, fake_ha: FakeHA) -> None:
    toolbox = Toolbox(ha)
    assert "call_service" not in tool_names(toolbox)

    content, is_error = toolbox.run("call_service", {"domain": "light", "service": "x"})
    assert is_error
    assert not any(method == "POST" for method, _, _ in fake_ha.calls)


def test_list_entities_filters_by_domain(ha: HomeAssistantClient) -> None:
    content, is_error = Toolbox(ha).run("list_entities", {"domain": "light"})
    assert not is_error
    assert json.loads(content) == [
        {"entity_id": "light.kitchen", "state": "off", "name": "Kitchen light"}
    ]


def test_list_services_unknown_domain_is_error(ha: HomeAssistantClient) -> None:
    content, is_error = Toolbox(ha).run("list_services", {"domain": "lock"})
    assert is_error
    assert "Unknown domain" in content


def test_rejected_action_does_not_call_ha(
    ha: HomeAssistantClient, fake_ha: FakeHA
) -> None:
    toolbox = Toolbox(ha, allow_actions=True, confirm=lambda *_: False)
    content, is_error = toolbox.run(
        "call_service", {"domain": "light", "service": "turn_on"}
    )
    assert not is_error
    assert json.loads(content)["status"] == "rejected"
    assert not any(method == "POST" for method, _, _ in fake_ha.calls)


def test_approved_action_calls_ha(ha: HomeAssistantClient, fake_ha: FakeHA) -> None:
    seen: list[Any] = []

    def approve(domain: str, service: str, data: dict[str, Any]) -> bool:
        seen.append((domain, service, data))
        return True

    toolbox = Toolbox(ha, allow_actions=True, confirm=approve)
    data = {"entity_id": "light.kitchen"}
    content, _ = toolbox.run(
        "call_service", {"domain": "light", "service": "turn_on", "data": data}
    )
    assert seen == [("light", "turn_on", data)]
    assert json.loads(content) == {"status": "done", "changed": ["light.kitchen"]}
