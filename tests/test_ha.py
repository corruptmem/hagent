from __future__ import annotations

import httpx
import pytest

from hagent.ha import HomeAssistantClient, HomeAssistantError

from .conftest import FakeHA


def test_ping_sends_bearer_token(ha: HomeAssistantClient, fake_ha: FakeHA) -> None:
    assert ha.ping() == "API running."
    assert fake_ha.calls == [("GET", "/api/", None)]


def test_get_state_returns_entity(ha: HomeAssistantClient) -> None:
    assert ha.get_state("climate.lounge")["attributes"]["current_temperature"] == 19.5


def test_unknown_entity_raises(ha: HomeAssistantClient) -> None:
    with pytest.raises(HomeAssistantError, match="404"):
        ha.get_state("light.nowhere")


def test_bad_token_raises(fake_ha: FakeHA) -> None:
    client = HomeAssistantClient(
        "http://ha.test:8123", "wrong", transport=httpx.MockTransport(fake_ha.handler)
    )
    with pytest.raises(HomeAssistantError, match="401"):
        client.ping()


def test_call_service_posts_data(ha: HomeAssistantClient, fake_ha: FakeHA) -> None:
    ha.call_service("light", "turn_on", {"entity_id": "light.kitchen"})
    assert fake_ha.calls[-1] == (
        "POST",
        "/api/services/light/turn_on",
        {"entity_id": "light.kitchen"},
    )
