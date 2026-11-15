"""The HA registries that the scope check needs.

The scope check resolves area, label, device and floor targets to entity IDs
(design section 8). For this it needs the HA entity, device, area, floor and
label registries, and the list of HA services. ``HARegistry`` loads them
over the WebSocket API and keeps a ``RegistrySnapshot``.

Terms:

- **Registry snapshot**: one consistent copy of the registries and the
  service list. It never changes. A reload makes a new snapshot.
- **Stale**: the snapshot may not match HA. This is true before the first
  load, after a disconnect, and from a ``*_registry_updated`` event until the
  reload that follows it ends.

The scope check fails closed: it denies a call that needs registry data
while the registry is stale.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from hagent_app.ha.client import Event, HAClient, HAError, Subscription

_LOG = logging.getLogger(__name__)

# The events that change the data in a snapshot.
REGISTRY_EVENTS = (
    "entity_registry_updated",
    "device_registry_updated",
    "area_registry_updated",
    "floor_registry_updated",
    "label_registry_updated",
    "service_registered",
    "service_removed",
)

type ReloadListener = Callable[["RegistrySnapshot"], Awaitable[None]]


class RegistryUnavailableError(Exception):
    """The registry is stale, so the scope check cannot run."""


@dataclass(frozen=True, slots=True)
class EntityEntry:
    """One entity registry entry, with the fields that the check uses."""

    entity_id: str
    device_id: str | None = None
    area_id: str | None = None
    labels: frozenset[str] = frozenset()
    hidden: bool = False
    disabled: bool = False
    entity_category: str | None = None
    name: str | None = None

    @property
    def indirect_target(self) -> bool:
        """True if an area, device, floor or label target includes it.

        HA leaves out hidden entities, disabled entities and entities with
        an entity category (config or diagnostic) when it resolves such a
        target. The check does the same.
        """
        return not (self.hidden or self.disabled or self.entity_category)


@dataclass(frozen=True, slots=True)
class DeviceEntry:
    """One device registry entry."""

    id: str
    area_id: str | None = None
    labels: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class AreaEntry:
    """One area registry entry."""

    id: str
    floor_id: str | None = None
    labels: frozenset[str] = frozenset()
    name: str | None = None


@dataclass(frozen=True, slots=True)
class ServiceInfo:
    """What the ``get_services`` reply declares for one service.

    - ``target_domains``: the entity domains that the declared entity target
      accepts. ``None`` means that the service declares no entity target.
    - ``any_domain``: an entity target filter has no domain, so the target
      accepts entities of every domain.
    - ``fields``: the declared data fields. Sections are flattened.
    """

    target_domains: frozenset[str] | None = None
    any_domain: bool = False
    fields: frozenset[str] = frozenset()

    def accepts_target(self, entity_domain: str) -> bool:
        """True if the declared entity target accepts the domain."""
        if self.target_domains is None:
            return False
        return self.any_domain or entity_domain in self.target_domains


@dataclass(frozen=True, slots=True)
class RegistrySnapshot:
    """One consistent copy of the HA registries and services."""

    entities: Mapping[str, EntityEntry] = field(
        default_factory=lambda: MappingProxyType({})
    )
    devices: Mapping[str, DeviceEntry] = field(
        default_factory=lambda: MappingProxyType({})
    )
    areas: Mapping[str, AreaEntry] = field(default_factory=lambda: MappingProxyType({}))
    floors: frozenset[str] = frozenset()
    labels: frozenset[str] = frozenset()
    services: Mapping[str, frozenset[str]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    service_info: Mapping[str, ServiceInfo] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def has_service(self, domain: str, service: str) -> bool:
        """True if HA has the service."""
        return service in self.services.get(domain, frozenset())

    def info(self, domain: str, service: str) -> ServiceInfo | None:
        """The declared metadata of the service, or ``None`` if it is missing."""
        return self.service_info.get(f"{domain}.{service}")

    # Resolution. These follow HA's target resolution (helpers/target.py).

    def entities_of_devices(
        self, device_ids: Iterable[str], *, via_area: str | None = None
    ) -> set[str]:
        """The entities of the devices that an indirect target includes.

        With ``via_area``, the devices were found through that area. An
        entity with its own area is then included only through that area.
        """
        wanted = set(device_ids)
        found = set()
        for entry in self.entities.values():
            if entry.device_id not in wanted or not entry.indirect_target:
                continue
            if via_area is not None and entry.area_id not in (None, via_area):
                continue
            found.add(entry.entity_id)
        return found

    def entities_of_areas(self, area_ids: Iterable[str]) -> set[str]:
        """The entities in the areas, directly or through their device."""
        found: set[str] = set()
        for area_id in set(area_ids):
            for entry in self.entities.values():
                if entry.area_id == area_id and entry.indirect_target:
                    found.add(entry.entity_id)
            devices = [d.id for d in self.devices.values() if d.area_id == area_id]
            found |= self.entities_of_devices(devices, via_area=area_id)
        return found

    def areas_of_floors(self, floor_ids: Iterable[str]) -> set[str]:
        """The areas on the floors."""
        wanted = set(floor_ids)
        return {a.id for a in self.areas.values() if a.floor_id in wanted}

    def entities_of_labels(self, label_ids: Iterable[str]) -> set[str]:
        """The entities that a label target includes.

        A label includes the entities, the devices and the areas that have
        the label.
        """
        wanted = set(label_ids)
        found = {
            e.entity_id
            for e in self.entities.values()
            if e.labels & wanted and e.indirect_target
        }
        devices = [d.id for d in self.devices.values() if d.labels & wanted]
        found |= self.entities_of_devices(devices)
        areas = [a.id for a in self.areas.values() if a.labels & wanted]
        found |= self.entities_of_areas(areas)
        return found

    def all_entities_of_device(self, device_id: str) -> set[str]:
        """Every entity of a device, also hidden and disabled ones."""
        return {e.entity_id for e in self.entities.values() if e.device_id == device_id}


def _labels(value: Any) -> frozenset[str]:
    if not isinstance(value, list):
        return frozenset()
    return frozenset(str(v) for v in value if isinstance(v, str))


def _opt(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _field_names(fields: Any) -> frozenset[str] | None:
    """The declared field names. A section adds its child fields."""
    if not isinstance(fields, Mapping):
        return None
    names: set[str] = set()
    for name, spec in fields.items():
        if not isinstance(name, str):
            return None
        if (
            isinstance(spec, Mapping)
            and "selector" not in spec
            and isinstance(spec.get("fields"), Mapping)
        ):
            children = _field_names(spec["fields"])
            if children is None:
                return None
            names |= children
        else:
            names.add(name)
    return frozenset(names)


def _target(target: Any) -> tuple[frozenset[str] | None, bool]:
    """The entity domains of a declared target, and the any-domain flag.

    An unknown shape returns ``(None, False)``: no entity target.
    """
    if not isinstance(target, Mapping) or "entity" not in target:
        return None, False
    filters = target["entity"]
    if filters is None:
        filters = [{}]
    elif isinstance(filters, Mapping):
        filters = [filters]
    if not isinstance(filters, list):
        return None, False
    domains: set[str] = set()
    any_domain = False
    for raw in filters:
        item = {} if raw is None else raw
        if not isinstance(item, Mapping):
            return None, False
        domain = item.get("domain")
        if domain is None:
            # No domain filter (for example only an integration filter).
            any_domain = True
        elif isinstance(domain, str):
            domains.add(domain)
        elif isinstance(domain, list) and all(isinstance(d, str) for d in domain):
            domains |= set(domain)
        else:
            return None, False
    return frozenset(domains), any_domain


def parse_service_info(description: Any) -> ServiceInfo | None:
    """Parse one service description. Return ``None`` if the shape is unknown."""
    # HA always sends "fields" (helpers/service.py), so a description
    # without it is not a description that the check can trust.
    if not isinstance(description, Mapping) or "fields" not in description:
        return None
    raw = description["fields"]
    fields = _field_names({} if raw is None else raw)
    if fields is None:
        return None
    target_domains, any_domain = _target(description.get("target"))
    return ServiceInfo(target_domains, any_domain, fields)


def parse_snapshot(  # noqa: PLR0913
    *,
    entities: Iterable[Mapping[str, Any]],
    devices: Iterable[Mapping[str, Any]],
    areas: Iterable[Mapping[str, Any]],
    floors: Iterable[Mapping[str, Any]],
    labels: Iterable[Mapping[str, Any]],
    services: Mapping[str, Any],
) -> RegistrySnapshot:
    """Build a snapshot from the replies of the HA registry commands."""
    entity_map = {}
    for item in entities:
        entity_id = item.get("entity_id")
        if not isinstance(entity_id, str):
            continue
        entity_map[entity_id] = EntityEntry(
            entity_id=entity_id,
            device_id=_opt(item.get("device_id")),
            area_id=_opt(item.get("area_id")),
            labels=_labels(item.get("labels")),
            hidden=item.get("hidden_by") is not None,
            disabled=item.get("disabled_by") is not None,
            entity_category=_opt(item.get("entity_category")),
            name=_opt(item.get("name")) or _opt(item.get("original_name")),
        )
    device_map = {}
    for item in devices:
        device_id = item.get("id")
        if isinstance(device_id, str):
            device_map[device_id] = DeviceEntry(
                id=device_id,
                area_id=_opt(item.get("area_id")),
                labels=_labels(item.get("labels")),
            )
    area_map = {}
    for item in areas:
        area_id = item.get("area_id")
        if isinstance(area_id, str):
            area_map[area_id] = AreaEntry(
                id=area_id,
                floor_id=_opt(item.get("floor_id")),
                labels=_labels(item.get("labels")),
                name=_opt(item.get("name")),
            )
    service_map = {
        str(domain): frozenset(str(s) for s in names)
        for domain, names in services.items()
        if isinstance(names, Mapping)
    }
    info_map = {}
    for domain, names in services.items():
        if not isinstance(names, Mapping):
            continue
        for name, description in names.items():
            info = parse_service_info(description)
            if info is not None:
                info_map[f"{domain}.{name}"] = info
    return RegistrySnapshot(
        entities=MappingProxyType(entity_map),
        devices=MappingProxyType(device_map),
        areas=MappingProxyType(area_map),
        floors=frozenset(
            str(f["floor_id"]) for f in floors if isinstance(f.get("floor_id"), str)
        ),
        labels=frozenset(
            str(lb["label_id"]) for lb in labels if isinstance(lb.get("label_id"), str)
        ),
        services=MappingProxyType(service_map),
        service_info=MappingProxyType(info_map),
    )


class HARegistry:
    """The live registry snapshot, kept current over the WebSocket API.

    Event handlers only mark the snapshot stale and start a reload task.
    They do not wait, because the HA client calls them on its reader task.
    """

    def __init__(self, ha: HAClient) -> None:
        self._ha = ha
        self._snapshot: RegistrySnapshot | None = None
        self._stale = True
        self._dirty = False
        self._task: asyncio.Task[None] | None = None
        self._subs: list[Subscription] = []
        self._listeners: list[ReloadListener] = []
        self._loaded = asyncio.Event()
        self.reload_count = 0

    @property
    def stale(self) -> bool:
        """True while the snapshot may not match HA."""
        return self._stale or self._snapshot is None

    def current(self) -> RegistrySnapshot:
        """The current snapshot. Raise if it is stale."""
        if self.stale or self._snapshot is None:
            msg = "the HA registry data is not current"
            raise RegistryUnavailableError(msg)
        return self._snapshot

    def last(self) -> RegistrySnapshot | None:
        """The last snapshot, also if it is stale. For best-effort use only."""
        return self._snapshot

    def add_reload_listener(self, listener: ReloadListener) -> None:
        """Call ``listener(snapshot)`` after each successful reload."""
        self._listeners.append(listener)

    async def wait_loaded(self) -> None:
        """Wait until a reload ends and the snapshot is current."""
        while self.stale:
            self._loaded.clear()
            await self._loaded.wait()

    async def start(self) -> None:
        """Subscribe to the registry events and to connect events."""
        for event_type in REGISTRY_EVENTS:
            self._subs.append(
                await self._ha.subscribe_events(self._on_event, event_type)
            )
        self._ha.add_connect_listener(self._on_connect)
        self._ha.add_disconnect_listener(self._on_disconnect)
        if self._ha.connected:
            self._schedule()

    async def stop(self) -> None:
        """Remove the subscriptions and stop a running reload."""
        subs, self._subs = self._subs, []
        for sub in subs:
            await sub.unsubscribe()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _on_event(self, _event: Event) -> None:
        self._stale = True
        self._schedule()

    async def _on_connect(self) -> None:
        self._stale = True
        self._schedule()

    async def _on_disconnect(self) -> None:
        self._stale = True

    def _schedule(self) -> None:
        self._dirty = True
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._reload_loop(), name="ha-registry")

    async def _reload_loop(self) -> None:
        while self._dirty:
            self._dirty = False
            try:
                snapshot = await self._load()
            except (HAError, TimeoutError) as err:
                _LOG.warning("cannot load the HA registries: %s", err)
                return
            if self._dirty:
                # Another change arrived during the load. Load again.
                continue
            self._snapshot = snapshot
            self._stale = False
            self.reload_count += 1
            self._loaded.set()
            for listener in list(self._listeners):
                try:
                    await listener(snapshot)
                except Exception:
                    _LOG.exception("registry reload listener failed")

    async def _load(self) -> RegistrySnapshot:
        cmd = self._ha.command
        entities = await cmd({"type": "config/entity_registry/list"})
        devices = await cmd({"type": "config/device_registry/list"})
        areas = await cmd({"type": "config/area_registry/list"})
        floors = await cmd({"type": "config/floor_registry/list"})
        labels = await cmd({"type": "config/label_registry/list"})
        services = await cmd({"type": "get_services"})
        return parse_snapshot(
            entities=entities or [],
            devices=devices or [],
            areas=areas or [],
            floors=floors or [],
            labels=labels or [],
            services=services or {},
        )
