"""A sample home for the scope tests. All names are placeholders.

Layout:

- floor ``ground``: areas ``kitchen`` and ``lounge``;
- floor ``upstairs``: area ``bedroom``, with label ``quiet``;
- device ``DEV_KITCHEN`` in ``kitchen``: ``light.kitchen`` and the config
  entity ``number.kitchen_level``; ``light.kitchen_spot`` belongs to the
  device but has its own area, ``lounge``;
- device ``DEV_BEDROOM`` in ``bedroom``: ``light.bedroom``;
- device ``DEV_THERMO`` in ``lounge``, with label ``heating``:
  ``climate.lounge`` and ``sensor.lounge_temp``;
- ``light.lounge`` and ``media_player.lounge`` in ``lounge``, no device;
- ``switch.kettle`` in ``kitchen``, no device;
- ``light.hall``, ``lock.front``, ``media_player.other``, ``script.morning``
  and ``automation.wake`` have no area.
"""

from __future__ import annotations

import copy
from typing import Any

from hagent_app.tools.registry import RegistrySnapshot, parse_snapshot

from helpers.fake_ha import FakeHA, make_context, now_iso

DEV_KITCHEN = "0" * 31 + "1"
DEV_BEDROOM = "0" * 31 + "2"
DEV_THERMO = "0" * 31 + "3"

ENTITIES: list[dict[str, Any]] = [
    {"entity_id": "light.kitchen", "device_id": DEV_KITCHEN},
    {
        "entity_id": "number.kitchen_level",
        "device_id": DEV_KITCHEN,
        "entity_category": "config",
    },
    {"entity_id": "light.kitchen_spot", "device_id": DEV_KITCHEN, "area_id": "lounge"},
    {"entity_id": "light.bedroom", "device_id": DEV_BEDROOM},
    {"entity_id": "climate.lounge", "device_id": DEV_THERMO},
    {"entity_id": "sensor.lounge_temp", "device_id": DEV_THERMO},
    {"entity_id": "light.lounge", "area_id": "lounge"},
    {"entity_id": "media_player.lounge", "area_id": "lounge"},
    {"entity_id": "switch.kettle", "area_id": "kitchen"},
    {"entity_id": "light.hall"},
    {"entity_id": "lock.front"},
    {"entity_id": "media_player.other"},
    {"entity_id": "script.morning"},
    {"entity_id": "automation.wake"},
    {"entity_id": "input_boolean.guest"},
]

DEVICES: list[dict[str, Any]] = [
    {"id": DEV_KITCHEN, "area_id": "kitchen", "labels": []},
    {"id": DEV_BEDROOM, "area_id": "bedroom", "labels": []},
    {"id": DEV_THERMO, "area_id": "lounge", "labels": ["heating"]},
]

AREAS: list[dict[str, Any]] = [
    {"area_id": "kitchen", "floor_id": "ground", "labels": [], "name": "Kitchen"},
    {"area_id": "lounge", "floor_id": "ground", "labels": [], "name": "Lounge"},
    {"area_id": "bedroom", "floor_id": "upstairs", "labels": ["quiet"]},
]

FLOORS = [{"floor_id": "ground"}, {"floor_id": "upstairs"}]
LABELS = [{"label_id": "quiet"}, {"label_id": "heating"}]


def _fields(names: list[str]) -> dict[str, Any]:
    return {name: {"selector": {"text": None}} for name in names}


def _svc(
    target: str | None = None,
    fields: list[str] | None = None,
    section: list[str] | None = None,
) -> dict[str, Any]:
    """A ``get_services`` description, in the shape that HA sends.

    ``target`` is an entity domain, ``"*"`` for an entity target with no
    domain filter, or ``None`` for no target. ``section`` adds a collapsed
    section of fields, as ``light.turn_on`` has.
    """
    description: dict[str, Any] = {"fields": _fields(fields or [])}
    if section:
        description["fields"]["advanced_fields"] = {
            "collapsed": True,
            "fields": _fields(section),
        }
    if target == "*":
        description["target"] = {"entity": [{"integration": "test"}]}
    elif target is not None:
        description["target"] = {"entity": {"domain": target}}
    return description


# The service descriptions of the sample home. The recorder, zwave_js and
# group descriptions follow the HA Core 2026.6 services.yaml files.
SERVICES: dict[str, dict[str, dict[str, Any]]] = {
    "light": {
        "turn_on": _svc("light", ["brightness", "transition"], ["flash", "effect"]),
        "turn_off": _svc("light", ["transition", "flash"]),
        "toggle": _svc("light", ["brightness", "transition"]),
    },
    "switch": {"turn_on": _svc("switch"), "turn_off": _svc("switch")},
    "climate": {"set_temperature": _svc("climate", ["temperature", "hvac_mode"])},
    "lock": {"lock": _svc("lock", ["code"]), "unlock": _svc("lock", ["code"])},
    "number": {"set_value": _svc("number", ["value"])},
    "media_player": {
        "join": _svc("media_player", ["group_members"]),
        "play_media": _svc("media_player", ["media_content_id", "media_content_type"]),
    },
    "input_boolean": {
        "turn_on": _svc("input_boolean"),
        "turn_off": _svc("input_boolean"),
    },
    "persistent_notification": {
        "create": _svc(None, ["message", "title", "notification_id"])
    },
    "conversation": {"process": _svc(None, ["text", "language", "agent_id"])},
    # An integration service whose entity target has no domain filter.
    "remote_bridge": {"send": _svc("*", ["command"])},
    # Indirect domains.
    "script": {"turn_on": _svc("script", ["variables"]), "morning": _svc()},
    "automation": {
        "trigger": _svc("automation", ["skip_condition"]),
        "turn_off": _svc("automation", ["stop_actions"]),
    },
    "scene": {
        "turn_on": _svc("scene", ["transition"]),
        "apply": _svc(None, ["entities", "transition"]),
    },
    "homeassistant": {"turn_on": _svc("*"), "restart": _svc()},
    "shell_command": {"run": _svc()},
    "rest_command": {"call": _svc()},
    "python_script": {"run": _svc()},
    "pyscript": {"run": _svc()},
    "hassio": {"addon_stop": _svc(None, ["addon"])},
    "notify": {
        "notify": _svc(None, ["message", "title", "target", "data"]),
        "send_message": _svc("notify", ["message", "title"]),
    },
    "mqtt": {"publish": _svc(None, ["topic", "payload", "qos", "retain"])},
    "recorder": {
        "purge": _svc(None, ["keep_days", "repack", "apply_filter"]),
        "purge_entities": _svc(
            None, ["entity_id", "domains", "entity_globs", "keep_days"]
        ),
    },
    "zwave_js": {
        "multicast_set_value": _svc(
            None,
            [
                "area_id",
                "device_id",
                "entity_id",
                "broadcast",
                "command_class",
                "endpoint",
                "property",
                "property_key",
                "options",
                "value",
            ],
        )
    },
    "zha": {"permit": _svc(None, ["duration", "ieee"])},
    "matter": {"open_commissioning_window": _svc(None, ["device_id"])},
    "knx": {"send": _svc(None, ["address", "payload", "type"])},
    "group": {
        "set": _svc(
            None,
            [
                "object_id",
                "name",
                "icon",
                "entities",
                "add_entities",
                "remove_entities",
                "all",
            ],
        ),
        "remove": _svc(None, ["object_id"]),
    },
    "logger": {"set_level": _svc()},
    "system_log": {"clear": _svc()},
    "backup": {"create": _svc()},
    "hagent": {"invoke": _svc(None, ["agent_id", "text"])},
}


def _entry(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "device_id": None,
        "area_id": None,
        "labels": [],
        "hidden_by": None,
        "disabled_by": None,
        "entity_category": None,
        **item,
    }


def services() -> dict[str, dict[str, dict[str, Any]]]:
    """A deep copy of the service descriptions."""
    return copy.deepcopy(SERVICES)


def snapshot() -> RegistrySnapshot:
    """The registry snapshot of the sample home."""
    return parse_snapshot(
        entities=[_entry(e) for e in ENTITIES],
        devices=DEVICES,
        areas=AREAS,
        floors=FLOORS,
        labels=LABELS,
        services=services(),
    )


def populate(ha: FakeHA) -> None:
    """Fill a fake HA with the sample home."""
    ha.registry["entity"] = [_entry(e) for e in ENTITIES]
    ha.registry["device"] = [dict(d) for d in DEVICES]
    ha.registry["area"] = [dict(a) for a in AREAS]
    ha.registry["floor"] = [dict(f) for f in FLOORS]
    ha.registry["label"] = [dict(lb) for lb in LABELS]
    ha.services = services()
    stamp = now_iso()
    for item in ENTITIES:
        entity_id = item["entity_id"]
        ha.states[entity_id] = {
            "entity_id": entity_id,
            "state": "off",
            "attributes": {"friendly_name": entity_id.split(".", 1)[1]},
            "last_changed": stamp,
            "last_updated": stamp,
            "context": make_context(),
        }
