"""The management API routes for agents. ``docs/api.md`` describes them."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web

from hagent_app.agents.definition import AgentDefinition, default_definition_data
from hagent_app.agents.models import (
    Agent,
    AgentError,
    AgentVersion,
    FieldChange,
    StoreChoice,
)
from hagent_app.agents.service import AgentService
from hagent_app.agents.store import parse_definition
from hagent_app.http.errors import ApiError, read_json

_STATUS = {
    "agent_not_found": 404,
    "version_not_found": 404,
    "name_conflict": 409,
    "version_conflict": 409,
    "private_store_in_use": 409,
    "definition_invalid": 422,
    "private_store_choice_required": 400,
}

_ID = "{agent_id:[a-f0-9]{32}}"
_VERSION = "{version:[0-9]{1,9}}"

type Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def agent_json(agent: Agent) -> dict[str, Any]:
    """The JSON form of an agent with its current definition."""
    return {
        "id": agent.id,
        "name": agent.name,
        "enabled": agent.enabled,
        "version": agent.version,
        "created_at": agent.created_at,
        "updated_at": agent.updated_at,
        "private_store": (
            {"id": agent.private_store.id, "name": agent.private_store.name}
            if agent.private_store
            else None
        ),
        "definition": agent.definition.to_data(),
    }


def _summary_json(agent: Agent) -> dict[str, Any]:
    data = agent_json(agent)
    del data["definition"]
    return data


def version_json(version: AgentVersion, *, definition: bool = True) -> dict[str, Any]:
    """The JSON form of a definition version."""
    data: dict[str, Any] = {
        "version": version.version,
        "change": version.change,
        "source_agent_id": version.source_agent_id,
        "source_version": version.source_version,
        "created_at": version.created_at,
    }
    if definition:
        data["definition"] = version.definition.to_data()
    return data


def _change_json(change: FieldChange) -> dict[str, Any]:
    return {"path": change.path, "old": change.old, "new": change.new}


def _object(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ApiError(400, "invalid_body", "the body must be a JSON object")
    return body


def _definition(body: dict[str, Any]) -> AgentDefinition:
    raw = body.get("definition")
    if not isinstance(raw, dict):
        raise ApiError(400, "invalid_body", "the body needs a definition object")
    return parse_definition(json.dumps(raw))


def _optional_int(body: dict[str, Any], key: str) -> int | None:
    value = body.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApiError(400, "invalid_body", f"{key} must be an integer")
    return value


def _int_query(request: web.Request, key: str) -> int:
    try:
        return int(request.query[key])
    except (KeyError, ValueError) as err:
        raise ApiError(
            400, "invalid_query", f"the query needs an integer {key!r}"
        ) from err


class AgentsApi:
    """The routes of the agents area. They call ``AgentService``."""

    def __init__(self, service: Callable[[], AgentService]) -> None:
        self._service = service

    def routes(self) -> list[web.RouteDef]:
        """The route table. ``HttpServer.add_routes`` takes it."""
        h = self._wrap
        return [
            web.get("/api/agents/schema", h(self.schema)),
            web.get("/api/agents/defaults", h(self.defaults)),
            web.get("/api/agents", h(self.index)),
            web.post("/api/agents", h(self.create)),
            web.get(f"/api/agents/{_ID}", h(self.get)),
            web.put(f"/api/agents/{_ID}", h(self.update)),
            web.delete(f"/api/agents/{_ID}", h(self.delete)),
            web.put(f"/api/agents/{_ID}/enabled", h(self.set_enabled)),
            web.post(f"/api/agents/{_ID}/copy", h(self.copy)),
            web.get(f"/api/agents/{_ID}/versions", h(self.versions)),
            web.get(f"/api/agents/{_ID}/versions/{_VERSION}", h(self.version)),
            web.post(f"/api/agents/{_ID}/versions/{_VERSION}/restore", h(self.restore)),
            web.get(f"/api/agents/{_ID}/diff", h(self.diff)),
        ]

    @staticmethod
    def _wrap(handler: Handler) -> Handler:
        async def wrapped(request: web.Request) -> web.StreamResponse:
            try:
                return await handler(request)
            except AgentError as err:
                status = _STATUS.get(err.code, 400)
                raise ApiError(status, err.code, err.message, err.details) from err

        return wrapped

    async def schema(self, _request: web.Request) -> web.Response:
        """GET /api/agents/schema: the JSON schema of a definition."""
        return web.json_response(
            AgentDefinition.model_json_schema(by_alias=True, mode="validation")
        )

    async def defaults(self, _request: web.Request) -> web.Response:
        """GET /api/agents/defaults: a definition with every default value."""
        return web.json_response({"definition": default_definition_data()})

    async def index(self, _request: web.Request) -> web.Response:
        """GET /api/agents."""
        agents = await self._service().list_agents()
        return web.json_response({"agents": [_summary_json(a) for a in agents]})

    async def create(self, request: web.Request) -> web.Response:
        """POST /api/agents."""
        body = _object(await read_json(request))
        agent = await self._service().create(_definition(body))
        return web.json_response(agent_json(agent), status=201)

    async def get(self, request: web.Request) -> web.Response:
        """GET /api/agents/{id}."""
        agent = await self._service().get(request.match_info["agent_id"])
        return web.json_response(agent_json(agent))

    async def update(self, request: web.Request) -> web.Response:
        """PUT /api/agents/{id}."""
        body = _object(await read_json(request))
        agent = await self._service().update(
            request.match_info["agent_id"],
            _definition(body),
            _optional_int(body, "base_version"),
        )
        return web.json_response(agent_json(agent))

    async def set_enabled(self, request: web.Request) -> web.Response:
        """PUT /api/agents/{id}/enabled."""
        body = _object(await read_json(request))
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            raise ApiError(400, "invalid_body", "enabled must be true or false")
        agent = await self._service().set_enabled(
            request.match_info["agent_id"], enabled
        )
        return web.json_response(agent_json(agent))

    async def copy(self, request: web.Request) -> web.Response:
        """POST /api/agents/{id}/copy."""
        body = _object(await read_json(request)) if request.can_read_body else {}
        name = body.get("name")
        if name is not None and not isinstance(name, str):
            raise ApiError(400, "invalid_body", "name must be a string")
        agent = await self._service().copy(request.match_info["agent_id"], name)
        return web.json_response(agent_json(agent), status=201)

    async def delete(self, request: web.Request) -> web.Response:
        """DELETE /api/agents/{id}?private_store=keep|delete."""
        choice = request.query.get("private_store")
        await self._service().delete(
            request.match_info["agent_id"], store_choice(choice)
        )
        return web.json_response({"deleted": True, "private_store": choice})

    async def versions(self, request: web.Request) -> web.Response:
        """GET /api/agents/{id}/versions."""
        versions = await self._service().versions(request.match_info["agent_id"])
        return web.json_response(
            {"versions": [version_json(v, definition=False) for v in versions]}
        )

    async def version(self, request: web.Request) -> web.Response:
        """GET /api/agents/{id}/versions/{version}."""
        found = await self._service().version(
            request.match_info["agent_id"], int(request.match_info["version"])
        )
        return web.json_response(version_json(found))

    async def diff(self, request: web.Request) -> web.Response:
        """GET /api/agents/{id}/diff?from=A&to=B."""
        old, new = _int_query(request, "from"), _int_query(request, "to")
        changes = await self._service().diff(request.match_info["agent_id"], old, new)
        return web.json_response(
            {"from": old, "to": new, "changes": [_change_json(c) for c in changes]}
        )

    async def restore(self, request: web.Request) -> web.Response:
        """POST /api/agents/{id}/versions/{version}/restore."""
        body = _object(await read_json(request)) if request.can_read_body else {}
        agent = await self._service().restore(
            request.match_info["agent_id"],
            int(request.match_info["version"]),
            _optional_int(body, "base_version"),
        )
        return web.json_response(agent_json(agent))


def store_choice(value: str | None) -> StoreChoice | None:
    """The private-store choice of a delete request, or ``None``."""
    if value == "keep":
        return "keep"
    if value == "delete":
        return "delete"
    return None
