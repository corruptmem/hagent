"""The ``ha`` tool group (design section 8).

Read tools need the ``read``, ``control`` or ``admin`` level. They read the
state cache, or send a history or statistics command that names only
entities in the read scope. ``ha.call_service`` needs ``control``,
``admin`` or at least one output. The admin tools need ``admin``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field, JsonValue, StringConstraints

from hagent_app.agents.definition import AgentDefinition
from hagent_app.ha.client import HAClient
from hagent_app.tools.admin import CHANGE_COMMANDS, READ_COMMANDS
from hagent_app.tools.base import Args, Prepared, Tool, ToolContext, ToolEnv
from hagent_app.tools.registry import RegistrySnapshot, RegistryUnavailableError
from hagent_app.tools.scope import (
    AgentScope,
    PreparedCall,
    ScopeDeniedError,
    admin_entities,
    agent_scope,
    check_read,
    check_service_call,
)

_LOG = logging.getLogger(__name__)

EntityIdArg = Annotated[
    str, StringConstraints(pattern=r"^[a-z0-9_]+\.[a-z0-9_]+$", max_length=255)
]
EntityIds = Annotated[list[EntityIdArg], Field(min_length=1, max_length=50)]
Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]+$", max_length=100)]


def snapshot_or_none(env: ToolEnv) -> RegistrySnapshot | None:
    """The current registry snapshot, or ``None`` while it is stale."""
    return None if env.registry.stale else env.registry.current()


def scope_of(ctx: ToolContext, env: ToolEnv) -> AgentScope:
    """The resolved scope of the agent. Deny while the registry is stale."""
    try:
        return agent_scope(ctx.definition, snapshot_or_none(env))
    except RegistryUnavailableError as err:
        raise ScopeDeniedError("registry_unavailable", str(err)) from err


def require_states(env: ToolEnv) -> None:
    """Deny while the state cache is not loaded or may not match HA.

    The group check and the data scan read the cache. An empty or old cache
    would let them miss entities, so the tools fail closed.
    """
    if not env.states.ready or env.states.stale:
        raise ScopeDeniedError("states_unavailable", "the HA state data is not current")


def _state_data(env: ToolEnv, entity_id: str) -> dict[str, Any] | None:
    state = env.states.get(entity_id)
    if state is None:
        return None
    return {
        "entity_id": state.entity_id,
        "state": state.state,
        "attributes": dict(state.attributes),
        "last_changed": state.last_changed.isoformat(),
        "last_updated": state.last_updated.isoformat(),
    }


class _ReadTool(Tool):
    group = "ha"

    def available(self, definition: AgentDefinition) -> bool:
        return definition.tools.ha.level in {"read", "control", "admin"}


# ------------------------------------------------------------- read tools


class GetStateArgs(Args):
    """Arguments of ``ha.get_state``."""

    entity_id: EntityIdArg


class GetState(_ReadTool):
    """Read the current state of one entity."""

    name = "ha.get_state"
    description = "Read the current state and attributes of one entity."
    args_model = GetStateArgs

    async def authorize(
        self, ctx: ToolContext, env: ToolEnv, args: GetStateArgs
    ) -> Prepared:
        check_read(scope_of(ctx, env), [args.entity_id])
        require_states(env)
        return Prepared({"entity_id": args.entity_id}, args.entity_id)

    async def run(self, ctx: ToolContext, env: ToolEnv, prepared: Prepared) -> Any:
        data = _state_data(env, prepared.payload)
        if data is None:
            return {"entity_id": prepared.payload, "state": None}
        return data


class FindEntitiesArgs(Args):
    """Arguments of ``ha.find_entities``."""

    query: Annotated[str, StringConstraints(max_length=200)] | None = None
    domain: Slug | None = None
    area_id: Annotated[str, StringConstraints(max_length=128)] | None = None
    limit: Annotated[int, Field(ge=1, le=200)] = 50


class FindEntities(_ReadTool):
    """Find entities in the read scope by text, domain or area."""

    name = "ha.find_entities"
    description = (
        "Find entities in your scope by words in the entity ID or name, "
        "by domain, or by area."
    )
    args_model = FindEntitiesArgs

    async def authorize(
        self, ctx: ToolContext, env: ToolEnv, args: FindEntitiesArgs
    ) -> Prepared:
        scope = scope_of(ctx, env)
        if not scope.can_read:
            raise ScopeDeniedError("level", "the agent has no HA read access")
        require_states(env)
        reg = snapshot_or_none(env)
        if args.area_id is not None and reg is None:
            raise ScopeDeniedError(
                "registry_unavailable", "the HA registry data is not current"
            )
        known = {s.entity_id for s in env.states}
        if reg is not None:
            known |= set(reg.entities)
        area_entities = (
            reg.entities_of_areas([args.area_id])
            if reg is not None and args.area_id is not None
            else None
        )
        words = (args.query or "").lower().split()
        candidates = [
            entity_id
            for entity_id in sorted(known)
            if scope.read.contains(entity_id)
            and (not args.domain or entity_id.split(".", 1)[0] == args.domain)
            and (area_entities is None or entity_id in area_entities)
        ]
        if words and env.entities is not None:
            # Hybrid ranking over the scope-filtered candidates only.
            try:
                ranked = await env.entities.rank_entities(
                    args.query or "", candidates, args.limit
                )
            except Exception:
                _LOG.warning("entity search failed; using the word match")
            else:
                allowed = set(candidates)
                return Prepared(
                    args.model_dump(),
                    tuple(e for e in ranked if e in allowed)[: args.limit],
                )
        found = []
        for entity_id in candidates:
            if words:
                state = env.states.get(entity_id)
                name = str(state.attributes.get("friendly_name", "")) if state else ""
                entry = reg.entities.get(entity_id) if reg is not None else None
                text = f"{entity_id} {name} {entry.name if entry else ''}".lower()
                if not all(w in text for w in words):
                    continue
            found.append(entity_id)
            if len(found) >= args.limit:
                break
        return Prepared(args.model_dump(), tuple(found))

    async def run(self, ctx: ToolContext, env: ToolEnv, prepared: Prepared) -> Any:
        results = []
        for entity_id in prepared.payload:
            state = env.states.get(entity_id)
            results.append(
                {
                    "entity_id": entity_id,
                    "name": state.attributes.get("friendly_name") if state else None,
                    "state": state.state if state else None,
                }
            )
        return results


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _filter_keys(result: Any, allowed: set[str]) -> Any:
    """Drop every entry that the scope check did not permit."""
    if not isinstance(result, Mapping):
        return {}
    return {k: v for k, v in result.items() if k in allowed}


class HistoryArgs(Args):
    """Arguments of ``ha.get_history``."""

    entity_ids: EntityIds
    start: datetime
    end: datetime | None = None


class GetHistory(_ReadTool):
    """Read the state history of entities in the read scope."""

    name = "ha.get_history"
    description = "Read the state history of up to 50 entities in a time range."
    args_model = HistoryArgs

    async def authorize(
        self, ctx: ToolContext, env: ToolEnv, args: HistoryArgs
    ) -> Prepared:
        check_read(scope_of(ctx, env), args.entity_ids)
        command = {
            "type": "history/history_during_period",
            "start_time": _iso(args.start),
            "end_time": _iso(args.end),
            "entity_ids": sorted(set(args.entity_ids)),
            "minimal_response": True,
            "no_attributes": True,
            "significant_changes_only": False,
        }
        if args.end is None:
            del command["end_time"]
        return Prepared(command, command, entities=frozenset(args.entity_ids))

    async def run(self, ctx: ToolContext, env: ToolEnv, prepared: Prepared) -> Any:
        result = await env.ha.command(prepared.payload)
        return _filter_keys(result, set(prepared.entities))


class StatisticsArgs(Args):
    """Arguments of ``ha.get_statistics``."""

    statistic_ids: EntityIds
    start: datetime
    end: datetime | None = None
    period: Literal["5minute", "hour", "day", "week", "month"] = "hour"


class GetStatistics(_ReadTool):
    """Read long-term statistics of entities in the read scope."""

    name = "ha.get_statistics"
    description = (
        "Read long-term statistics (mean, min, max, sum) of up to 50 entities."
    )
    args_model = StatisticsArgs

    async def authorize(
        self, ctx: ToolContext, env: ToolEnv, args: StatisticsArgs
    ) -> Prepared:
        # Only entity statistics: an external statistic ID has a colon and
        # fails the argument pattern.
        check_read(scope_of(ctx, env), args.statistic_ids)
        command = {
            "type": "recorder/statistics_during_period",
            "start_time": _iso(args.start),
            "end_time": _iso(args.end),
            "statistic_ids": sorted(set(args.statistic_ids)),
            "period": args.period,
        }
        if args.end is None:
            del command["end_time"]
        return Prepared(command, command, entities=frozenset(args.statistic_ids))

    async def run(self, ctx: ToolContext, env: ToolEnv, prepared: Prepared) -> Any:
        result = await env.ha.command(prepared.payload)
        return _filter_keys(result, set(prepared.entities))


# ---------------------------------------------------------- service calls


class CallServiceArgs(Args):
    """Arguments of ``ha.call_service``."""

    domain: Slug
    service: Slug
    target: dict[str, JsonValue] | None = None
    data: dict[str, JsonValue] | None = None
    return_response: bool = False


def group_members(env: ToolEnv) -> dict[str, tuple[str, ...]]:
    """The members of every group entity, from the ``entity_id`` attribute."""
    members = {}
    for state in env.states:
        value = state.attributes.get("entity_id")
        if isinstance(value, (list, tuple)):
            names = tuple(v.lower() for v in value if isinstance(v, str))
            if names:
                members[state.entity_id] = names
    return members


def _snapshot_states(env: ToolEnv, entity_ids: frozenset[str]) -> dict[str, Any]:
    return {e: _state_data(env, e) for e in sorted(entity_ids)}


class CallService(Tool):
    """Call an HA service on entities in the control scope or the outputs."""

    name = "ha.call_service"
    group = "ha"
    description = (
        "Call a Home Assistant service. Name the target entities. The call "
        "must stay inside your scope and outputs."
    )
    args_model = CallServiceArgs
    changes_ha = True
    writes = True

    def available(self, definition: AgentDefinition) -> bool:
        return definition.tools.ha.level in {"control", "admin"} or bool(
            definition.outputs
        )

    def level(self, definition: AgentDefinition) -> str:
        level = definition.tools.ha.level
        return level if level in {"control", "admin"} else "outputs"

    async def authorize(
        self, ctx: ToolContext, env: ToolEnv, args: CallServiceArgs
    ) -> Prepared:
        scope = scope_of(ctx, env)
        require_states(env)
        reg = snapshot_or_none(env)
        known = frozenset(s.entity_id for s in env.states)
        members = group_members(env)
        try:
            call = check_service_call(
                scope,
                reg,
                known,
                args.domain,
                args.service,
                data=args.data,
                target=args.target,
                return_response=args.return_response,
                group_members=members,
            )
        except RegistryUnavailableError as err:
            raise ScopeDeniedError("registry_unavailable", str(err)) from err
        previous = None
        note = None
        approval_required = False
        entities = call.entities | call.data_entities
        if call.path == "admin":
            found = admin_entities(reg, known, args.data, args.target, members)
            entities = found.entities
            previous = {"states": _snapshot_states(env, entities)}
            note = "the states of the named entities before the call"
            if not found.complete:
                note += "; the entity scan did not finish, so the list can be short"
                # Fail closed: the approval check cannot see every entity.
                approval_required = bool(ctx.definition.approval.entities)
        return Prepared(
            call.request(),
            call,
            services=frozenset({call.service_name}),
            entities=entities,
            previous=previous,
            previous_note=note,
            approval_required=approval_required,
        )

    async def run(self, ctx: ToolContext, env: ToolEnv, prepared: Prepared) -> Any:
        call: PreparedCall = prepared.payload
        result = await env.ha.call_service(
            call.domain,
            call.service,
            call.data,
            target=call.target,
            return_response=call.return_response,
        )
        return {"context_id": result.context_id, "response": result.response}


# ------------------------------------------------------------ admin tools


class AdminArgs(Args):
    """Arguments of the admin tools: one HA WebSocket command."""

    command: dict[str, JsonValue]


def _command(args: AdminArgs, allowed: Any) -> dict[str, Any]:
    command = {k: v for k, v in args.command.items() if k != "id"}
    kind = command.get("type")
    if not isinstance(kind, str) or kind not in allowed:
        raise ScopeDeniedError(
            "command_not_permitted", f"the admin tools do not accept {kind!r}"
        )
    return command


class _AdminTool(Tool):
    group = "ha"

    def available(self, definition: AgentDefinition) -> bool:
        return definition.tools.ha.level == "admin"

    def _require_admin(self, ctx: ToolContext) -> None:
        if ctx.definition.tools.ha.level != "admin":
            raise ScopeDeniedError("level", "this tool needs the admin level")


class AdminRead(_AdminTool):
    """Read HA configuration with one WebSocket command (``admin`` only)."""

    name = "ha.admin_read"
    description = (
        "Read HA configuration: registries, helpers, dashboards, automation "
        "and script configuration. Admin level only."
    )
    args_model = AdminArgs

    async def authorize(
        self, ctx: ToolContext, env: ToolEnv, args: AdminArgs
    ) -> Prepared:
        self._require_admin(ctx)
        command = _command(args, READ_COMMANDS)
        return Prepared(command, command)

    async def run(self, ctx: ToolContext, env: ToolEnv, prepared: Prepared) -> Any:
        return await env.ha.command(prepared.payload)


class AdminChange(_AdminTool):
    """Change HA configuration with one WebSocket command (``admin`` only)."""

    name = "ha.admin_change"
    description = (
        "Change HA configuration: registry entries, helpers and dashboards. "
        "Admin level only. The audit log keeps the previous version."
    )
    args_model = AdminArgs
    changes_ha = True
    writes = True

    async def authorize(
        self, ctx: ToolContext, env: ToolEnv, args: AdminArgs
    ) -> Prepared:
        self._require_admin(ctx)
        command = _command(args, CHANGE_COMMANDS)
        reader = CHANGE_COMMANDS[command["type"]]
        try:
            previous = await reader(env.ha, command)
        except Exception as err:
            # Fail closed: no change without its previous version.
            raise ScopeDeniedError(
                "previous_unavailable",
                f"cannot read the previous version: {err}",
            ) from err
        return Prepared(
            command,
            command,
            previous=previous.value,
            previous_note=previous.note,
        )

    async def run(self, ctx: ToolContext, env: ToolEnv, prepared: Prepared) -> Any:
        ha: HAClient = env.ha
        return {"result": await ha.command(prepared.payload)}


HA_TOOLS: tuple[Tool, ...] = (
    GetState(),
    FindEntities(),
    GetHistory(),
    GetStatistics(),
    CallService(),
    AdminRead(),
    AdminChange(),
)
