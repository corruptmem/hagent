"""The WebSocket commands that the ``admin`` tools accept.

``admin`` is a blanket scope (design section 8). The admin tools still
accept only commands from two allowlists:

- ``READ_COMMANDS`` read HA configuration. They change nothing.
- ``CHANGE_COMMANDS`` change HA configuration. Each one has a reader for
  the previous version of the changed item, because the audit log keeps
  the previous version of each ``admin`` change. A command that creates a
  new item has no previous version.

A command outside both lists is denied. HA changes automation, script and
scene configuration only through its REST API. The app has no REST client
yet, so the admin tools cannot change those files. An agent can still call
the services of those domains, for example ``automation.reload``, through
``ha.call_service``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from hagent_app.ha.client import HAClient, HACommandError

HELPER_DOMAINS = (
    "input_boolean",
    "input_number",
    "input_text",
    "input_select",
    "input_datetime",
    "input_button",
    "counter",
    "timer",
    "schedule",
)

READ_COMMANDS = frozenset(
    {
        "get_config",
        "get_services",
        "config/entity_registry/list",
        "config/entity_registry/get",
        "config/device_registry/list",
        "config/area_registry/list",
        "config/floor_registry/list",
        "config/label_registry/list",
        "lovelace/config",
        "lovelace/dashboards/list",
        "automation/config",
        "script/config",
    }
    | {f"{domain}/list" for domain in HELPER_DOMAINS}
)

type Reader = Callable[[HAClient, Mapping[str, Any]], Awaitable["Previous"]]


@dataclass(frozen=True, slots=True)
class Previous:
    """The previous version of an item, or a note that says why it is empty."""

    value: Any = None
    note: str | None = None


NEW_ITEM = Previous(note="the command creates a new item")


async def _new(_ha: HAClient, _msg: Mapping[str, Any]) -> Previous:
    return NEW_ITEM


def _find_in_list(list_type: str, item_key: str, msg_key: str) -> Reader:
    async def read(ha: HAClient, msg: Mapping[str, Any]) -> Previous:
        wanted = msg.get(msg_key)
        items = await ha.command({"type": list_type}) or []
        for item in items:
            if isinstance(item, Mapping) and item.get(item_key) == wanted:
                return Previous(dict(item))
        return Previous(note=f"no item with {msg_key} {wanted!r}")

    return read


async def _entity(ha: HAClient, msg: Mapping[str, Any]) -> Previous:
    try:
        value = await ha.command(
            {"type": "config/entity_registry/get", "entity_id": msg.get("entity_id")}
        )
    except HACommandError as err:
        if err.code == "not_found":
            return Previous(note="the entity is not in the registry")
        raise
    return Previous(value)


async def _lovelace(ha: HAClient, msg: Mapping[str, Any]) -> Previous:
    query: dict[str, Any] = {"type": "lovelace/config", "force": False}
    if msg.get("url_path") is not None:
        query["url_path"] = msg["url_path"]
    try:
        return Previous(await ha.command(query))
    except HACommandError as err:
        if err.code == "config_not_found":
            return Previous(note="the dashboard had no stored configuration")
        raise


def _change_commands() -> dict[str, Reader]:
    commands: dict[str, Reader] = {
        "config/entity_registry/update": _entity,
        "config/entity_registry/remove": _entity,
        "config/device_registry/update": _find_in_list(
            "config/device_registry/list", "id", "device_id"
        ),
        "lovelace/config/save": _lovelace,
        "lovelace/config/delete": _lovelace,
        "lovelace/dashboards/create": _new,
        "lovelace/dashboards/update": _find_in_list(
            "lovelace/dashboards/list", "id", "dashboard_id"
        ),
        "lovelace/dashboards/delete": _find_in_list(
            "lovelace/dashboards/list", "id", "dashboard_id"
        ),
    }
    for registry in ("area", "floor", "label"):
        key = f"{registry}_id"
        list_type = f"config/{registry}_registry/list"
        commands[f"config/{registry}_registry/create"] = _new
        commands[f"config/{registry}_registry/update"] = _find_in_list(
            list_type, key, key
        )
        commands[f"config/{registry}_registry/delete"] = _find_in_list(
            list_type, key, key
        )
    for domain in HELPER_DOMAINS:
        commands[f"{domain}/create"] = _new
        for action in ("update", "delete"):
            commands[f"{domain}/{action}"] = _find_in_list(
                f"{domain}/list", "id", f"{domain}_id"
            )
    return commands


CHANGE_COMMANDS: Mapping[str, Reader] = _change_commands()
