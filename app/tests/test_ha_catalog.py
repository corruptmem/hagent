"""The read-only HA catalogue for the UI pickers (``api/ha/catalog``)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from hagent_app import ha_catalog
from hagent_app.app import App, default_components
from hagent_app.config import Config
from hagent_app.ha_catalog import REGISTRY_COMMANDS
from hagent_app.http import HttpServer
from helpers.fake_ha import FakeCommandError, FakeHA
from helpers.wait import wait_until

REGISTRIES: dict[str, Any] = {
    "config/entity_registry/list": [
        {
            "entity_id": "light.test_lamp",
            "name": None,
            "original_name": "Lamp",
            "device_id": "dev1",
            "area_id": None,
            "labels": ["lbl1"],
            "disabled_by": None,
        },
        {
            "entity_id": "sensor.test_disabled",
            "name": "Disabled sensor",
            "device_id": None,
            "area_id": "area2",
            "labels": [],
            "disabled_by": "user",
        },
    ],
    "config/device_registry/list": [
        {
            "id": "dev1",
            "name": "Lamp device",
            "name_by_user": None,
            "area_id": "area1",
            "labels": [],
        },
    ],
    "config/area_registry/list": [
        {"area_id": "area1", "name": "Room A", "floor_id": "floor1"},
        {"area_id": "area2", "name": "Room B", "floor_id": None},
    ],
    "config/floor_registry/list": [{"floor_id": "floor1", "name": "Ground"}],
    "config/label_registry/list": [{"label_id": "lbl1", "name": "Test label"}],
    "get_services": {"light": {"turn_on": {}, "turn_off": {}}, "notify": {"x": {}}},
}

# auth/current_user: the approvals component reads the HA user of the app.
READ_COMMANDS = {
    "get_states",
    "subscribe_events",
    "auth/current_user",
    *REGISTRY_COMMANDS.values(),
}


def _reply(result: Any) -> Any:
    def handler(_ha: FakeHA, _msg: Any) -> Any:
        return result

    return handler


@pytest.fixture
async def catalog(fake_ha: FakeHA, tmp_path: Path) -> AsyncIterator[str]:
    for kind, result in REGISTRIES.items():
        fake_ha.handlers[kind] = _reply(result)
    fake_ha.set_state("light.test_lamp", "on", {"friendly_name": "Lamp state name"})
    fake_ha.set_state("input_boolean.test", "off")
    # The tools component reads the same registries for its own snapshot.
    # Leave it out, so that the command counts show only catalog commands.
    components = [c for c in default_components() if c.name != "tools"]
    config = Config(
        ha_url=fake_ha.url,
        ha_token=fake_ha.token,
        data_dir=tmp_path,
        port=0,
        options={"ingress": {"allowed_addresses": ["127.0.0.1"]}},
    )
    app = App(config, components)
    ctx = await app.start()
    await ctx.states.wait_ready()
    http = components[-1]
    assert isinstance(http, HttpServer)
    try:
        yield f"http://127.0.0.1:{http.port}/api/ha/catalog"
    finally:
        await app.stop()


async def _get(url: str) -> dict[str, Any]:
    async with aiohttp.ClientSession() as session, session.get(url) as resp:
        assert resp.status == 200
        return await resp.json()  # type: ignore[no-any-return]


async def test_catalog_joins_states_and_registries(
    catalog: str, fake_ha: FakeHA
) -> None:
    body = await _get(catalog)
    assert body["stale"] is False
    entities = {e["entity_id"]: e for e in body["entities"]}
    assert entities["light.test_lamp"] == {
        "entity_id": "light.test_lamp",
        "name": "Lamp state name",
        "domain": "light",
        "state": "on",
        "device_id": "dev1",
        "area_id": "area1",
        "labels": ["lbl1"],
        "disabled": False,
    }
    assert entities["sensor.test_disabled"]["state"] is None
    assert entities["sensor.test_disabled"]["disabled"] is True
    assert entities["input_boolean.test"]["name"] == "input_boolean.test"
    assert body["devices"] == [
        {"id": "dev1", "name": "Lamp device", "area_id": "area1", "labels": []}
    ]
    assert [a["id"] for a in body["areas"]] == ["area1", "area2"]
    assert body["floors"] == [{"id": "floor1", "name": "Ground"}]
    assert body["labels"] == [{"id": "lbl1", "name": "Test label"}]
    assert body["domains"] == ["input_boolean", "light", "sensor"]
    assert body["services"] == ["light.turn_off", "light.turn_on", "notify.x"]


async def test_catalog_sends_only_read_commands(catalog: str, fake_ha: FakeHA) -> None:
    """The endpoint never changes HA: it sends only fixed list commands."""
    for query in ("", "?type=call_service", "?domain=light&service=turn_on"):
        await _get(catalog + query)
    kinds = {c.get("type") for c in fake_ha.commands}
    assert kinds <= READ_COMMANDS
    assert not fake_ha.commands_of("call_service")
    for command in fake_ha.commands:
        assert set(command) <= {"id", "type", "event_type"}


async def test_catalog_caches_registries(
    catalog: str, fake_ha: FakeHA, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _get(catalog)
    await _get(catalog)
    assert len(fake_ha.commands_of("config/device_registry/list")) == 1
    monkeypatch.setattr(ha_catalog.component, "REGISTRY_TTL", 0.0)
    await _get(catalog)
    assert len(fake_ha.commands_of("config/device_registry/list")) == 2


async def test_catalog_when_ha_fails_or_is_offline(
    catalog: str, fake_ha: FakeHA, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _get(catalog)
    monkeypatch.setattr(ha_catalog.component, "REGISTRY_TTL", 0.0)

    def fail(_ha: FakeHA, _msg: Any) -> Any:
        raise FakeCommandError("unknown_error", "registry failed")

    fake_ha.handlers["config/label_registry/list"] = fail
    body = await _get(catalog)
    assert body["stale"] is True
    # The app keeps the last good data of a failed registry.
    assert body["labels"] == [{"id": "lbl1", "name": "Test label"}]

    await fake_ha.restart()
    body = await _get(catalog)
    assert body["stale"] is True
    assert "light.test_lamp" in {e["entity_id"] for e in body["entities"]}
    fake_ha.up()
    await wait_until(lambda: fake_ha.connection_count > 0, limit=5)


# ------------------------------------------------- HA users (task-20)

USER_A = "1" * 32
USER_B = "2" * 32
SYSTEM_USER = "3" * 32
AUTH_LIST = [
    {
        "id": USER_B,
        "username": "b_user",
        "name": "User B",
        "is_owner": False,
        "is_active": True,
        "system_generated": False,
        "group_ids": ["system-users"],
        "credentials": [{"type": "homeassistant"}],
    },
    {
        "id": USER_A,
        "username": "a_user",
        "name": "User A",
        "is_owner": True,
        "is_active": True,
        "system_generated": False,
        "group_ids": ["system-admin"],
        "credentials": [{"type": "homeassistant"}],
    },
    {
        "id": SYSTEM_USER,
        "name": "Supervisor",
        "is_active": True,
        "system_generated": True,
        "group_ids": ["system-admin"],
    },
    {"id": "4" * 32, "name": "Old", "is_active": False, "system_generated": False},
]


def _users_url(catalog: str) -> str:
    return catalog.replace("/api/ha/catalog", "/api/ha/users")


async def test_users_lists_people_without_credentials(
    catalog: str, fake_ha: FakeHA
) -> None:
    fake_ha.handlers["config/auth/list"] = _reply(AUTH_LIST)
    body = await _get(_users_url(catalog) + "?type=call_service")
    assert body["source"] == "auth"
    assert body["users"] == [
        {"id": USER_A, "name": "User A", "username": "a_user", "is_admin": True},
        {"id": USER_B, "name": "User B", "username": "b_user", "is_admin": False},
    ]
    assert "credentials" not in json.dumps(body)
    # Only the fixed read command reaches HA, without request values.
    sent = fake_ha.commands_of("config/auth/list")
    assert [set(c) for c in sent] == [{"id", "type"}]
    assert not fake_ha.commands_of("call_service")


async def test_users_fall_back_to_persons_when_ha_refuses(
    catalog: str, fake_ha: FakeHA
) -> None:
    def refuse(_ha: FakeHA, _msg: Any) -> Any:
        raise FakeCommandError("unauthorized", "Unauthorized")

    fake_ha.handlers["config/auth/list"] = refuse
    fake_ha.set_state("person.a", "home", {"friendly_name": "A", "user_id": USER_A})
    fake_ha.set_state("person.no_user", "home", {"friendly_name": "Nobody"})
    for _ in range(100):
        body = await _get(_users_url(catalog))
        if body["users"]:
            break
        await asyncio.sleep(0.02)
    assert body["source"] == "persons"
    assert body["reason"] == "HA refused config/auth/list"
    assert body["users"] == [
        {"id": USER_A, "name": "A", "username": None, "is_admin": None}
    ]


async def test_users_without_any_source_return_an_empty_list(
    catalog: str, fake_ha: FakeHA
) -> None:
    def refuse(_ha: FakeHA, _msg: Any) -> Any:
        raise FakeCommandError("unauthorized", "Unauthorized")

    fake_ha.handlers["config/auth/list"] = refuse
    body = await _get(_users_url(catalog))
    assert body == {
        "source": None,
        "reason": "HA refused config/auth/list",
        "users": [],
    }
