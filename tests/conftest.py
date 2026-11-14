from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from hagent.ha import HomeAssistantClient

STATES = [
    {
        "entity_id": "light.kitchen",
        "state": "off",
        "attributes": {"friendly_name": "Kitchen light"},
    },
    {
        "entity_id": "climate.lounge",
        "state": "heat",
        "attributes": {"friendly_name": "Lounge", "current_temperature": 19.5},
    },
]
SERVICES = [{"domain": "light", "services": {"turn_on": {"fields": {}}}}]


class FakeHA:
    """Records requests and answers like the Home Assistant REST API."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body))
        if request.headers.get("Authorization") != "Bearer test-token":
            return httpx.Response(401, text="401: Unauthorized")
        path = request.url.path.removeprefix("/api")
        if path == "/":
            return httpx.Response(200, json={"message": "API running."})
        if path == "/states":
            return httpx.Response(200, json=STATES)
        if path.startswith("/states/"):
            entity_id = path.removeprefix("/states/")
            for state in STATES:
                if state["entity_id"] == entity_id:
                    return httpx.Response(200, json=state)
            return httpx.Response(404, json={"message": "Entity not found."})
        if path == "/services":
            return httpx.Response(200, json=SERVICES)
        if path.startswith("/services/") and request.method == "POST":
            return httpx.Response(200, json=[STATES[0]])
        return httpx.Response(404)


@pytest.fixture
def fake_ha() -> FakeHA:
    return FakeHA()


@pytest.fixture
def ha(fake_ha: FakeHA) -> Iterator[HomeAssistantClient]:
    client = HomeAssistantClient(
        "http://ha.test:8123",
        "test-token",
        transport=httpx.MockTransport(fake_ha.handler),
    )
    yield client
    client.close()
