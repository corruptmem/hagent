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

SERVICES: dict[str, list[str]] = {
    "light": ["turn_on", "turn_off", "toggle"],
    "switch": ["turn_on", "turn_off"],
    "climate": ["set_temperature"],
    "lock": ["lock", "unlock"],
    "number": ["set_value"],
    "media_player": ["join", "play_media"],
    "script": ["turn_on", "morning"],
    "automation": ["trigger", "turn_off"],
    "scene": ["turn_on", "apply"],
    "homeassistant": ["turn_on", "restart"],
    "shell_command": ["run"],
    "rest_command": ["call"],
    "python_script": ["run"],
    "pyscript": ["run"],
    "hassio": ["addon_stop"],
    "notify": ["notify", "send_message"],
    "mqtt": ["publish"],
    "recorder": ["purge"],
    "conversation": ["process"],
    "input_boolean": ["turn_on", "turn_off"],
    "persistent_notification": ["create"],
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


def snapshot() -> RegistrySnapshot:
    """The registry snapshot of the sample home."""
    return parse_snapshot(
        entities=[_entry(e) for e in ENTITIES],
        devices=DEVICES,
        areas=AREAS,
        floors=FLOORS,
        labels=LABELS,
        services={d: {s: {} for s in names} for d, names in SERVICES.items()},
    )


def populate(ha: FakeHA) -> None:
    """Fill a fake HA with the sample home."""
    ha.registry["entity"] = [_entry(e) for e in ENTITIES]
    ha.registry["device"] = [dict(d) for d in DEVICES]
    ha.registry["area"] = [dict(a) for a in AREAS]
    ha.registry["floor"] = [dict(f) for f in FLOORS]
    ha.registry["label"] = [dict(lb) for lb in LABELS]
    ha.services = {d: {s: {} for s in names} for d, names in SERVICES.items()}
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
