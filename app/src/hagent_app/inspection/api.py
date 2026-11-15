"""The inspect routes of the management API (``docs/api.md``, "Inspect").

Every route is read-only. A route keyed by agent filters each statement by
the agent ID.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from aiohttp import web

from hagent_app.http.errors import ApiError
from hagent_app.inspection import queries
from hagent_app.providers.meter import local_now

if TYPE_CHECKING:
    from hagent_app.inspection.component import InspectComponent

_ID = "{agent_id:[a-f0-9]{32}}"
_INVOCATION = "{invocation_id:[a-f0-9]{32}}"
_BASE = "/api/inspect"

INVOCATION_STATUSES = frozenset(
    {"queued", "running", "ok", "fallback", "error", "cancelled"}
)
PATHS = frozenset({"rule", "agent_turn", "fallback"})
DECISIONS = frozenset({"allowed", "denied"})
MAX_LIMIT = 500
MAX_DAYS = 366
MAX_TEXT = 200


def _limit(request: web.Request, default: int = 100) -> int:
    text = request.query.get("limit", str(default))
    try:
        value = int(text)
    except ValueError:
        value = 0
    if not 1 <= value <= MAX_LIMIT:
        raise ApiError(400, "invalid_query", f"limit must be 1 to {MAX_LIMIT}")
    return value


def _choice(request: web.Request, name: str, allowed: frozenset[str]) -> str | None:
    value = request.query.get(name)
    if value is not None and value not in allowed:
        raise ApiError(400, "invalid_query", f"unknown {name} {value!r}")
    return value


def _text(request: web.Request, name: str) -> str | None:
    value = request.query.get(name)
    if value is None or value == "":
        return None
    if len(value) > MAX_TEXT:
        raise ApiError(400, "invalid_query", f"{name} is too long")
    return value


def _agent_id(request: web.Request) -> str | None:
    value = request.query.get("agent_id")
    if value is not None and (
        len(value) != 32 or any(c not in "0123456789abcdef" for c in value)  # noqa: PLR2004
    ):
        raise ApiError(400, "invalid_query", "agent_id must be 32 hex characters")
    return value


def _flag(request: web.Request, name: str) -> bool:
    value = request.query.get(name, "false")
    if value not in {"true", "false"}:
        raise ApiError(400, "invalid_query", f"{name} must be true or false")
    return value == "true"


class InspectApi:
    """The routes of the inspect area."""

    def __init__(self, component: InspectComponent) -> None:
        self._component = component

    def routes(self) -> list[web.RouteDef]:
        """The route table."""
        return [
            web.get(f"{_BASE}/invocations", self.invocations),
            web.get(f"{_BASE}/invocations/{_INVOCATION}", self.trace),
            web.get(f"{_BASE}/audit", self.audit),
            web.get(f"{_BASE}/schedules", self.schedules),
            web.get(f"{_BASE}/cost", self.cost),
            web.get(f"{_BASE}/agents/{_ID}/permissions", self.permissions),
        ]

    async def _run(self, fn: Any) -> Any:
        return await self._component.db().run(fn)

    async def invocations(self, request: web.Request) -> web.Response:
        """The invocation list, with filters and the history search."""
        filters = queries.InvocationFilter(
            agent_id=_agent_id(request),
            source=_text(request, "source"),
            path=_choice(request, "path", PATHS),
            status=_choice(request, "status", INVOCATION_STATUSES),
            query=_text(request, "q"),
            before=_text(request, "before"),
            dry_run=_flag(request, "dry_run"),
            limit=_limit(request),
        )

        def run(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            return queries.list_invocations(conn, filters)

        return web.json_response({"invocations": await self._run(run)})

    async def trace(self, request: web.Request) -> web.Response:
        """The full trace of one invocation."""
        invocation_id = request.match_info["invocation_id"]

        def run(conn: sqlite3.Connection) -> dict[str, Any] | None:
            return queries.invocation_trace(conn, invocation_id)

        found = await self._run(run)
        if found is None:
            raise ApiError(404, "invocation_not_found", "no invocation with this ID")
        return web.json_response(found)

    async def audit(self, request: web.Request) -> web.Response:
        """The audit log: changes to HA and denied calls, newest first."""
        before = request.query.get("before")
        if before is not None and not before.isdigit():
            raise ApiError(400, "invalid_query", "before must be an audit row ID")
        filters = queries.AuditFilter(
            agent_id=_agent_id(request),
            decision=_choice(request, "decision", DECISIONS),
            tool=_text(request, "tool"),
            before=None if before is None else int(before),
            limit=_limit(request),
        )

        def run(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            return queries.list_audit(conn, filters)

        return web.json_response({"entries": await self._run(run)})

    async def schedules(self, request: web.Request) -> web.Response:
        """Schedules and subscriptions, with their last runs and events."""
        agent_id = _agent_id(request)

        def run(conn: sqlite3.Connection) -> dict[str, Any]:
            return queries.schedules(conn, agent_id)

        return web.json_response(await self._run(run))

    async def cost(self, request: web.Request) -> web.Response:
        """Cost totals by agent, day, path and call type."""
        text = request.query.get("days", "30")
        days = int(text) if text.isdigit() else 0
        if not 1 <= days <= MAX_DAYS:
            raise ApiError(400, "invalid_query", f"days must be 1 to {MAX_DAYS}")
        agent_id = _agent_id(request)
        since = (local_now().date() - timedelta(days=days - 1)).isoformat()

        def run(conn: sqlite3.Connection) -> dict[str, Any]:
            return queries.cost(conn, since, agent_id)

        return web.json_response(await self._run(run))

    async def permissions(self, request: web.Request) -> web.Response:
        """The effective scopes of one agent and its denied calls."""
        agent = await self._component.agent(request.match_info["agent_id"])
        limit = _limit(request)
        view = self._component.permissions(agent)

        def run(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            return queries.denied_calls(conn, agent.id, limit)

        view["denied"] = await self._run(run)
        return web.json_response(view)
