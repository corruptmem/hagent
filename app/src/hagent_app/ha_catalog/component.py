"""The HA catalogue for the pickers of the management UI.

The catalogue joins the state cache with the HA registries. It sends only
the fixed read commands in ``REGISTRY_COMMANDS`` to HA. No request value
reaches HA, so the endpoint cannot change HA.

This endpoint is not an agent tool. Only the management UI uses it, and HA
has authenticated the user before Ingress forwards the request. Agent tools
read HA only through the tool executor, which checks the scope.

The component keeps the registry data for ``REGISTRY_TTL`` seconds. When HA
is offline, it returns the last data that it has, with ``stale`` set.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from aiohttp import web

from hagent_app.ha.client import HAError
from hagent_app.http.errors import ApiError

if TYPE_CHECKING:
    from hagent_app.app import AppContext

_LOG = logging.getLogger(__name__)

REGISTRY_TTL = 30.0
REGISTRY_COMMANDS: Mapping[str, str] = {
    "entities": "config/entity_registry/list",
    "devices": "config/device_registry/list",
    "areas": "config/area_registry/list",
    "floors": "config/floor_registry/list",
    "labels": "config/label_registry/list",
    "services": "get_services",
}
CATALOG_PATH = "/api/ha/catalog"


@dataclass
class _Registries:
    data: dict[str, Any] = field(default_factory=dict)
    loaded_at: float | None = None
    complete: bool = False


class CatalogComponent:
    """The ``api/ha/catalog`` route."""

    name = "ha_catalog"

    def __init__(self) -> None:
        self._ctx: AppContext | None = None
        self._registries = _Registries()
        self._lock = asyncio.Lock()

    def routes(self) -> list[web.RouteDef]:
        """The routes for ``HttpServer.add_routes``."""
        return [web.get(CATALOG_PATH, self._catalog)]

    async def start(self, ctx: AppContext) -> None:
        """Bind to the HA client and the state cache."""
        self._ctx = ctx

    async def stop(self) -> None:
        """Release the context."""
        self._ctx = None

    async def _catalog(self, _request: web.Request) -> web.Response:
        ctx = self._ctx
        if ctx is None:
            raise ApiError(503, "not_ready", "the catalog component is not running")
        registries = await self._load(ctx)
        body = build_catalog(ctx, registries.data)
        body["stale"] = ctx.states.stale or not registries.complete
        return web.json_response(body)

    async def _load(self, ctx: AppContext) -> _Registries:
        async with self._lock:
            current = self._registries
            fresh = (
                current.complete
                and current.loaded_at is not None
                and time.monotonic() - current.loaded_at < REGISTRY_TTL
            )
            if fresh or not ctx.ha.connected:
                return current
            data: dict[str, Any] = {}
            complete = True
            for key, command in REGISTRY_COMMANDS.items():
                try:
                    data[key] = await ctx.ha.command({"type": command})
                except (HAError, TimeoutError) as err:
                    _LOG.warning("HA command %s failed: %s", command, err)
                    data[key] = current.data.get(key)
                    complete = False
            self._registries = _Registries(data, time.monotonic(), complete)
            return self._registries


def _items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def build_catalog(ctx: AppContext, registries: Mapping[str, Any]) -> dict[str, Any]:
    """Join the state cache with the registry data."""
    devices = {
        str(d["id"]): {
            "id": str(d["id"]),
            "name": d.get("name_by_user") or d.get("name") or str(d["id"]),
            "area_id": d.get("area_id"),
            "labels": sorted(d.get("labels") or []),
        }
        for d in _items(registries.get("devices"))
        if d.get("id")
    }
    registry_entities = {
        str(e["entity_id"]): e
        for e in _items(registries.get("entities"))
        if e.get("entity_id")
    }
    entities: dict[str, dict[str, Any]] = {}
    for state in ctx.states:
        entities[state.entity_id] = _entity(
            state.entity_id,
            registry_entities.get(state.entity_id, {}),
            devices,
            state=state.state,
            friendly_name=state.attributes.get("friendly_name"),
        )
    for entity_id, entry in registry_entities.items():
        if entity_id not in entities:
            entities[entity_id] = _entity(entity_id, entry, devices)

    services = registries.get("services")
    service_names = sorted(
        f"{domain}.{service}"
        for domain, items in (services.items() if isinstance(services, dict) else ())
        if isinstance(items, dict)
        for service in items
    )
    return {
        "entities": sorted(entities.values(), key=lambda e: e["entity_id"]),
        "devices": sorted(devices.values(), key=lambda d: (d["name"], d["id"])),
        "areas": sorted(
            (
                {
                    "id": str(a["area_id"]),
                    "name": a.get("name") or str(a["area_id"]),
                    "floor_id": a.get("floor_id"),
                }
                for a in _items(registries.get("areas"))
                if a.get("area_id")
            ),
            key=lambda a: a["name"],
        ),
        "floors": sorted(
            (
                {"id": str(f["floor_id"]), "name": f.get("name") or str(f["floor_id"])}
                for f in _items(registries.get("floors"))
                if f.get("floor_id")
            ),
            key=lambda f: f["name"],
        ),
        "labels": sorted(
            (
                {"id": str(lb["label_id"]), "name": lb.get("name") or lb["label_id"]}
                for lb in _items(registries.get("labels"))
                if lb.get("label_id")
            ),
            key=lambda lb: lb["name"],
        ),
        "domains": sorted({e.split(".", 1)[0] for e in entities}),
        "services": service_names,
    }


def _entity(
    entity_id: str,
    entry: Mapping[str, Any],
    devices: Mapping[str, Mapping[str, Any]],
    *,
    state: str | None = None,
    friendly_name: Any = None,
) -> dict[str, Any]:
    device_id = entry.get("device_id")
    device = devices.get(str(device_id)) if device_id else None
    name = entry.get("name") or friendly_name or entry.get("original_name")
    return {
        "entity_id": entity_id,
        "name": str(name) if name else entity_id,
        "domain": entity_id.split(".", 1)[0],
        "state": state,
        "device_id": device_id,
        "area_id": entry.get("area_id") or (device or {}).get("area_id"),
        "labels": sorted(entry.get("labels") or []),
        "disabled": bool(entry.get("disabled_by")),
    }
