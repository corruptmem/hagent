"""The scope check (design sections 8 and 19).

The app has administrator access to HA. This module is the only barrier
between an agent and full access. It is pure and synchronous: it takes a
registry snapshot, the scope of an agent and a call, and it returns either a
denial or a rewritten call. It does no I/O.

Terms:

- **Resolved scope**: the set of entity IDs and domains that a scope
  permits, after area, device, floor and label targets are resolved.
- **Prepared call**: a service call that passed the check. Its target names
  explicit entity IDs only. HA therefore acts on exactly the entities that
  the check saw, even if the registry changes after the check.
- **Indirect service**: a service that runs other actions, for example
  ``script.turn_on``. A ``control`` scope permits one only by its exact name.

Rules for ``read`` and ``control``. The check denies a call when:

- the registry is stale and the call or the scope needs registry data;
- HA does not have the service;
- the target has an unknown key, a value that is not text, or an unknown
  device, area, floor or label;
- the target or the service data names ``all`` or ``none``, unless the scope
  sets ``all``;
- the service data contains a template (``{{``, ``{%`` or ``{#``);
- an entity in the target, or a known entity or device in the service data,
  is outside the scope;
- the service is indirect and the scope does not name it;
- the scope lists services and the service is not one of them;
- the call resolves to no entity, unless the scope names the service
  exactly. Services such as ``mqtt.publish`` or ``recorder.purge`` have no
  target, so this rule stops them;
- HA declares no metadata for the service (``get_services``);
- the call has target entities, and the service declares no entity target
  that accepts the domain of each one;
- an explicit target entity has a domain other than the service domain,
  unless the scope names the service exactly (``domain.service``, not
  ``domain.*``);
- the service data has a key that the service does not declare as a field.
  Some services take fields that select other things (domains, globs,
  broadcast flags). The field check stops undeclared keys, and the indirect
  list stops the services with such declared fields.

``group.set`` and ``group.remove`` name a group by ``object_id``. The check
treats it as the entity ``group.<object_id>``.

An output is a short form of ``control`` for one entity. A call whose
entities are all outputs passes without a ``control`` level, unless the
service is indirect or its domain differs from the domain of an output.

``admin`` is a blanket scope with no narrower form. The check passes every
``admin`` call unchanged.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from hagent_app.agents.definition import INDIRECT_DOMAINS, AgentDefinition, HAScope
from hagent_app.tools.registry import RegistrySnapshot, RegistryUnavailableError

# Use fullmatch: "$" in Python also matches before a final newline.
ENTITY_ID = re.compile(r"[a-z0-9_]+\.[a-z0-9_]+")
_NAME = re.compile(r"[a-z0-9_]+")
TARGET_KEYS = ("entity_id", "device_id", "area_id", "floor_id", "label_id")
_TEMPLATE_MARKS = ("{{", "{%", "{#")
_SPECIAL = frozenset({"all", "none"})
MAX_DEPTH = 12
MAX_GROUP_DEPTH = 5

type Level = Literal["none", "read", "control", "admin"]
type Path = Literal["control", "outputs", "admin"]


class ScopeDeniedError(Exception):
    """The scope does not permit the call. ``code`` is a stable identifier."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ResolvedScope:
    """The entity IDs and domains that a scope permits."""

    all: bool = False
    entities: frozenset[str] = frozenset()
    domains: frozenset[str] = frozenset()

    def contains(self, entity_id: str) -> bool:
        """True if the scope permits the entity."""
        if self.all or entity_id in self.entities:
            return True
        return entity_id.split(".", 1)[0] in self.domains

    def covers(self, entity_ids: Iterable[str]) -> bool:
        """True if the scope permits every entity."""
        return all(self.contains(e) for e in entity_ids)

    def entity_set(self, known: Iterable[str]) -> frozenset[str]:
        """The permitted entities among ``known``, plus the listed ones."""
        return frozenset(e for e in known if self.contains(e)) | self.entities


NOTHING = ResolvedScope()
EVERYTHING = ResolvedScope(all=True)


def scope_needs_registry(scope: HAScope) -> bool:
    """True if the scope uses devices, areas, floors or labels."""
    return bool(scope.devices or scope.areas or scope.floors or scope.labels)


def resolve_scope(scope: HAScope, reg: RegistrySnapshot | None) -> ResolvedScope:
    """Resolve a scope to entity IDs and domains.

    Raise ``RegistryUnavailableError`` if the scope needs registry data and
    ``reg`` is ``None``. Unknown devices, areas, floors and labels resolve
    to nothing.
    """
    if scope.all:
        return EVERYTHING
    entities = set(scope.entities)
    if scope_needs_registry(scope):
        if reg is None:
            msg = "the scope uses registry targets and the registry is not current"
            raise RegistryUnavailableError(msg)
        entities |= reg.entities_of_devices(scope.devices)
        areas = set(scope.areas) | reg.areas_of_floors(scope.floors)
        entities |= reg.entities_of_areas(areas)
        entities |= reg.entities_of_labels(scope.labels)
    return ResolvedScope(entities=frozenset(entities), domains=frozenset(scope.domains))


@dataclass(frozen=True, slots=True)
class AgentScope:
    """The resolved HA scope of one agent definition."""

    level: Level
    read: ResolvedScope
    control: ResolvedScope
    outputs: frozenset[str]
    services: tuple[str, ...] = ()

    @property
    def admin(self) -> bool:
        """True for the blanket ``admin`` scope."""
        return self.level == "admin"

    @property
    def can_call_services(self) -> bool:
        """True if the agent can call services at all."""
        return self.level in {"control", "admin"} or bool(self.outputs)

    @property
    def can_read(self) -> bool:
        """True if the agent has the HA read tools."""
        return self.level in {"read", "control", "admin"}


def agent_scope(
    definition: AgentDefinition, reg: RegistrySnapshot | None
) -> AgentScope:
    """Resolve the HA scope of a definition.

    The read scope is the ``read`` or ``control`` scope, plus the outputs
    and the inputs. Raise ``RegistryUnavailableError`` if resolution needs
    registry data and ``reg`` is ``None``.
    """
    ha = definition.tools.ha
    outputs = frozenset(definition.outputs)
    if ha.level == "admin":
        return AgentScope("admin", EVERYTHING, EVERYTHING, outputs)
    extra = set(outputs) | set(definition.inputs.entities)
    if definition.inputs.devices:
        if reg is None:
            msg = "the inputs use devices and the registry is not current"
            raise RegistryUnavailableError(msg)
        extra |= reg.entities_of_devices(definition.inputs.devices)
    base = resolve_scope(ha.scope, reg) if ha.level in {"read", "control"} else NOTHING
    read = ResolvedScope(
        all=base.all, entities=base.entities | extra, domains=base.domains
    )
    control = base if ha.level == "control" else NOTHING
    return AgentScope(ha.level, read, control, outputs, ha.services)


# ------------------------------------------------------------ read checks


def check_read(scope: AgentScope, entity_ids: Iterable[str]) -> None:
    """Deny unless the read scope permits every entity."""
    if not scope.can_read:
        raise ScopeDeniedError("level", "the agent has no HA read access")
    ids = list(entity_ids)
    for entity_id in ids:
        if not isinstance(entity_id, str) or not ENTITY_ID.fullmatch(entity_id):
            raise ScopeDeniedError("invalid_target", f"not an entity ID: {entity_id!r}")
    outside = [e for e in ids if not scope.read.contains(e)]
    if outside:
        raise ScopeDeniedError(
            "out_of_scope", f"outside the read scope: {', '.join(sorted(outside))}"
        )


# ---------------------------------------------------------- service calls


@dataclass(frozen=True, slots=True)
class PreparedCall:
    """A service call that passed the check.

    ``target`` names explicit entity IDs only (or ``all`` for a scope with
    ``all``). ``entities`` holds every entity that the call names, in the
    target and in the service data.
    """

    domain: str
    service: str
    data: Mapping[str, Any]
    target: Mapping[str, Any] | None
    entities: frozenset[str]
    path: Path
    return_response: bool = False
    data_entities: frozenset[str] = field(default=frozenset())

    @property
    def service_name(self) -> str:
        """The full service name, ``domain.service``."""
        return f"{self.domain}.{self.service}"

    def request(self) -> dict[str, Any]:
        """The call as JSON data, for the audit log."""
        return {
            "domain": self.domain,
            "service": self.service,
            "target": dict(self.target) if self.target else None,
            "data": dict(self.data),
            "return_response": self.return_response,
        }


def service_permitted(services: Collection[str], domain: str, service: str) -> bool:
    """True if a ``control`` services list permits the service.

    An empty list permits every service that is not indirect. An indirect
    service needs its exact name in the list.
    """
    name = f"{domain}.{service}"
    if domain in INDIRECT_DOMAINS:
        return name in services
    return not services or name in services or f"{domain}.*" in services


def _as_list(key: str, value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        return list(value)
    raise ScopeDeniedError("invalid_target", f"{key} must be text or a list of text")


@dataclass(slots=True)
class _DataRefs:
    entities: set[str] = field(default_factory=set)
    devices: set[str] = field(default_factory=set)
    special: bool = False


def _scan_text(
    value: str, reg: RegistrySnapshot, known: Collection[str], refs: _DataRefs
) -> None:
    """Find templates and entity or device IDs in one text value or key."""
    if any(mark in value for mark in _TEMPLATE_MARKS):
        raise ScopeDeniedError(
            "template", "templates are not permitted in service data"
        )
    # HA validators lower-case entity IDs and split text at commas
    # (cv.entity_ids), so the check does the same.
    for piece in value.split(","):
        name = piece.strip().lower()
        if ENTITY_ID.fullmatch(name) and (name in known or name in reg.entities):
            refs.entities.add(name)
        if name in reg.devices:
            refs.devices.add(name)


def _expand_groups(
    entity_ids: Iterable[str], members: Mapping[str, Collection[str]]
) -> frozenset[str]:
    """Add the members of group entities, also of nested groups.

    A group entity (for example a light group or a ``group.*`` entity)
    acts on its members. HA lists them in the ``entity_id`` attribute.
    """
    found = set(entity_ids)
    frontier = set(found)
    for _ in range(MAX_GROUP_DEPTH):
        new = {m for e in frontier for m in members.get(e, ())} - found
        if not new:
            return frozenset(found)
        found |= new
        frontier = new
    raise ScopeDeniedError("invalid_target", "the groups are nested too deep")


def _walk_data(  # noqa: PLR0912
    value: Any,
    reg: RegistrySnapshot,
    known: Collection[str],
    refs: _DataRefs,
    depth: int = 0,
) -> None:
    """Find the entity and device references in service data."""
    if depth > MAX_DEPTH:
        raise ScopeDeniedError("invalid_data", "the service data is nested too deep")
    if value is None or isinstance(value, (bool, int, float)):
        return
    if isinstance(value, str):
        _scan_text(value, reg, known, refs)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ScopeDeniedError("invalid_data", "service data keys must be text")
            if key == "entity_id":
                for name in _as_list(key, item):
                    if name in _SPECIAL:
                        refs.special = True
                    elif not ENTITY_ID.fullmatch(name):
                        raise ScopeDeniedError(
                            "invalid_target", f"not an entity ID: {name!r}"
                        )
                    else:
                        refs.entities.add(name)
                continue
            if key in TARGET_KEYS:
                raise ScopeDeniedError(
                    "invalid_target",
                    f"{key} is permitted only at the top level of the service data",
                )
            # Keys can name entities too, for example in scene.apply.
            _scan_text(key, reg, known, refs)
            _walk_data(item, reg, known, refs, depth + 1)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _walk_data(item, reg, known, refs, depth + 1)
        return
    raise ScopeDeniedError("invalid_data", "the service data is not JSON data")


def _check_names(domain: str, service: str) -> None:
    if not (_NAME.fullmatch(domain) and _NAME.fullmatch(service)):
        raise ScopeDeniedError(
            "invalid_service", f"not a valid service name: {domain}.{service}"
        )


def _check_declared(  # noqa: PLR0913
    reg: RegistrySnapshot,
    domain: str,
    service: str,
    data: Mapping[str, Any],
    entities: Collection[str],
    *,
    cross_domain_ok: bool,
) -> None:
    """Check the call against what HA declares for the service.

    ``entities`` are the target entities (on the outputs path, every entity
    that the call names). ``data`` is the service data without the target
    keys. Fail closed when the metadata is missing.
    """
    info = reg.info(domain, service)
    if info is None:
        raise ScopeDeniedError(
            "service_metadata",
            f"HA declares no metadata for {domain}.{service}; the call is denied",
        )
    for entity_id in sorted(entities):
        entity_domain = entity_id.split(".", 1)[0]
        if entity_domain != domain and not cross_domain_ok:
            raise ScopeDeniedError(
                "cross_domain",
                f"{domain}.{service} cannot act on {entity_id}: the domains differ. "
                "Only a control scope that names the service exactly permits this",
            )
        if not info.accepts_target(entity_domain):
            raise ScopeDeniedError(
                "target_not_declared",
                f"{domain}.{service} declares no entity target for "
                f"{entity_domain} entities",
            )
    unknown = sorted(set(data) - info.fields)
    if unknown:
        raise ScopeDeniedError(
            "unknown_field",
            f"{domain}.{service} does not declare the fields: {', '.join(unknown)}",
        )


def check_service_call(  # noqa: PLR0912, PLR0913, PLR0915
    scope: AgentScope,
    reg: RegistrySnapshot | None,
    known_entities: Collection[str],
    domain: str,
    service: str,
    *,
    data: Mapping[str, Any] | None = None,
    target: Mapping[str, Any] | None = None,
    return_response: bool = False,
    group_members: Mapping[str, Collection[str]] | None = None,
) -> PreparedCall:
    """Check a service call against the scope. Return the prepared call.

    ``known_entities`` is the set of entity IDs in the state cache.
    ``group_members`` maps each group entity to its members. Every member
    must be in the scope too. Raise ``ScopeDeniedError`` or
    ``RegistryUnavailableError``.
    """
    _check_names(domain, service)
    data = dict(data or {})
    target = dict(target or {})
    if scope.admin:
        return PreparedCall(
            domain,
            service,
            data,
            target or None,
            frozenset(),
            "admin",
            return_response,
        )
    if not scope.can_call_services:
        raise ScopeDeniedError("level", "the agent cannot call HA services")
    if reg is None:
        msg = "the HA registry data is not current"
        raise RegistryUnavailableError(msg)
    if not reg.has_service(domain, service):
        raise ScopeDeniedError(
            "unknown_service", f"HA has no service {domain}.{service}"
        )

    # The target and the target keys at the top level of the service data.
    unknown = set(target) - set(TARGET_KEYS)
    if unknown:
        raise ScopeDeniedError(
            "invalid_target", f"unknown target keys: {', '.join(sorted(unknown))}"
        )
    refs: dict[str, list[str]] = {key: [] for key in TARGET_KEYS}
    for key in TARGET_KEYS:
        if key in target:
            refs[key] += _as_list(key, target[key])
        if key in data:
            refs[key] += _as_list(key, data.pop(key))

    special = [e for e in refs["entity_id"] if e in _SPECIAL]
    explicit = {e for e in refs["entity_id"] if e not in _SPECIAL}
    for entity_id in explicit:
        if not ENTITY_ID.fullmatch(entity_id):
            raise ScopeDeniedError("invalid_target", f"not an entity ID: {entity_id!r}")
    for key, known, what in (
        ("device_id", reg.devices, "device"),
        ("area_id", reg.areas, "area"),
        ("floor_id", reg.floors, "floor"),
        ("label_id", reg.labels, "label"),
    ):
        missing = [v for v in refs[key] if v not in known]
        if missing:
            raise ScopeDeniedError(
                "invalid_target", f"unknown {what}: {', '.join(sorted(missing))}"
            )

    # Resolve indirect targets. Keep only the entities of the service domain,
    # as HA does for an entity service.
    indirect = reg.entities_of_devices(refs["device_id"])
    areas = set(refs["area_id"]) | reg.areas_of_floors(refs["floor_id"])
    indirect |= reg.entities_of_areas(areas)
    indirect |= reg.entities_of_labels(refs["label_id"])
    indirect = {e for e in indirect if e.split(".", 1)[0] == domain}
    entities = frozenset(explicit | indirect)
    registry_refs = any(refs[k] for k in TARGET_KEYS if k != "entity_id")
    if registry_refs and not entities and not special:
        # Do not drop a target in silence: HA would get a call with none.
        raise ScopeDeniedError(
            "invalid_target",
            f"the area, device, floor or label target has no {domain} entity",
        )

    # References inside the service data.
    data_refs = _DataRefs()
    _walk_data(data, reg, known_entities, data_refs)
    data_entities = set(data_refs.entities)
    for device_id in data_refs.devices:
        device_entities = reg.all_entities_of_device(device_id)
        if not device_entities:
            raise ScopeDeniedError(
                "out_of_scope", "the service data names a device with no entities"
            )
        data_entities |= device_entities
    if (special or data_refs.special) and not scope.control.all:
        raise ScopeDeniedError(
            "out_of_scope", "'all' and 'none' need a scope with all=true"
        )

    if domain == "group" and "object_id" in data:
        object_id = data["object_id"]
        if not isinstance(object_id, str) or not _NAME.fullmatch(object_id):
            raise ScopeDeniedError("invalid_data", "object_id must be a slug")
        data_entities.add(f"group.{object_id}")
        data_refs.entities.add(f"group.{object_id}")  # named in a denial

    refs_all = _expand_groups(entities | data_entities, group_members or {})
    indirect_service = domain in INDIRECT_DOMAINS
    in_control = scope.level == "control" and scope.control.covers(refs_all)
    permitted = service_permitted(scope.services, domain, service)

    named = f"{domain}.{service}" in scope.services
    if in_control and permitted:
        if not entities and not special and not named:
            raise ScopeDeniedError(
                "no_target",
                f"{domain}.{service} names no entity in the scope; "
                "the scope must name this service to permit it",
            )
        path: Path = "control"
    elif (
        entities
        and not special
        and not data_refs.special
        and refs_all <= scope.outputs
        and not indirect_service
    ):
        path = "outputs"
    elif in_control and indirect_service:
        raise ScopeDeniedError(
            "indirect_service",
            f"{domain}.{service} runs other actions; the scope must name it",
        )
    elif in_control:
        raise ScopeDeniedError(
            "service_not_permitted", f"the scope does not permit {domain}.{service}"
        )
    else:
        named_outside = sorted(
            e
            for e in explicit | data_refs.entities
            if not (scope.control.contains(e) or e in scope.outputs)
        )
        hidden = len([e for e in refs_all if not scope.control.contains(e)]) - len(
            named_outside
        )
        parts = []
        if named_outside:
            parts.append(", ".join(named_outside))
        if hidden > 0:
            parts.append(f"{hidden} entities from the area, device, floor or label")
        detail = "; ".join(parts) or "the call"
        if indirect_service and refs_all <= scope.outputs:
            raise ScopeDeniedError(
                "indirect_service",
                f"{domain}.{service} runs other actions; outputs do not permit it",
            )
        raise ScopeDeniedError("out_of_scope", f"outside the scope: {detail}")

    _check_declared(
        reg,
        domain,
        service,
        data,
        entities if path == "control" else refs_all,
        cross_domain_ok=named and path == "control",
    )

    if special:
        new_target: dict[str, Any] | None = {"entity_id": special[0]}
    elif entities:
        new_target = {"entity_id": sorted(entities)}
    else:
        new_target = None
    return PreparedCall(
        domain,
        service,
        data,
        new_target,
        entities,
        path,
        return_response,
        refs_all - entities,
    )


@dataclass(frozen=True, slots=True)
class AdminEntities:
    """The entities that an ``admin`` call names, as far as known.

    ``complete`` is false when the scan could not finish, for example with
    no current registry, with a template in the data, or with groups that
    are nested too deep. The approval check then fails closed.
    """

    entities: frozenset[str]
    complete: bool


def admin_entities(  # noqa: PLR0912
    reg: RegistrySnapshot | None,
    known_entities: Collection[str],
    data: Mapping[str, Any] | None,
    target: Mapping[str, Any] | None,
    group_members: Mapping[str, Collection[str]] | None = None,
) -> AdminEntities:
    """The entities that an ``admin`` call names, for approval and the audit log.

    This is best effort and never raises. It resolves the target keys,
    scans the service data for entity and device IDs and expands groups.
    """
    found: set[str] = set()
    complete = reg is not None
    data = dict(data or {})
    sources = [target or {}, {k: v for k, v in data.items() if k in TARGET_KEYS}]
    for source in sources:
        for key in TARGET_KEYS:
            value = source.get(key)
            values = [value] if isinstance(value, str) else value
            if values is None:
                continue
            if not isinstance(values, (list, tuple)):
                complete = False
                continue
            if not all(isinstance(v, str) for v in values):
                complete = False
            names = [v for v in values if isinstance(v, str)]
            if key == "entity_id":
                # HA splits text at commas, strips and lower-cases it
                # (cv.comp_entity_ids). An "all" or a bad piece makes the
                # scan incomplete, so that the approval check fails closed.
                for name in names:
                    for piece in name.split(","):
                        entity_id = piece.strip().lower()
                        if ENTITY_ID.fullmatch(entity_id):
                            found.add(entity_id)
                        else:
                            complete = False
            elif reg is not None:
                if key == "device_id":
                    found |= reg.entities_of_devices(names)
                elif key == "area_id":
                    found |= reg.entities_of_areas(names)
                elif key == "floor_id":
                    found |= reg.entities_of_areas(reg.areas_of_floors(names))
                else:
                    found |= reg.entities_of_labels(names)
    if reg is not None:
        refs = _DataRefs()
        rest = {k: v for k, v in data.items() if k not in TARGET_KEYS}
        try:
            _walk_data(rest, reg, known_entities, refs)
        except ScopeDeniedError:
            complete = False
        if refs.special:
            complete = False
        found |= refs.entities
        for device_id in refs.devices:
            found |= reg.all_entities_of_device(device_id)
    try:
        found = set(_expand_groups(found, group_members or {}))
    except ScopeDeniedError:
        complete = False
    return AdminEntities(frozenset(found), complete)
