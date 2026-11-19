"""The agents component: it owns ``AgentService`` and the agents routes."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from aiohttp import web

from hagent_app.agents.api import AgentsApi
from hagent_app.agents.service import AgentService
from hagent_app.http.errors import ApiError
from hagent_app.http.events import ChangeEvents

if TYPE_CHECKING:
    from hagent_app.app import AppContext


class AgentsComponent:
    """Agent definitions, versions and their management API routes."""

    name = "agents"

    def __init__(
        self,
        events: ChangeEvents,
        *,
        default_budget: Callable[[], float | None] | None = None,
    ) -> None:
        """``default_budget`` gives the settings default agent budget (task-20)."""
        self._events = events
        self._service: AgentService | None = None
        self._api = AgentsApi(self.service, default_budget)

    def service(self) -> AgentService:
        """The running service. Raise ``ApiError`` 503 before start."""
        if self._service is None:
            raise ApiError(503, "not_ready", "the agents component is not running")
        return self._service

    def routes(self) -> list[web.RouteDef]:
        """The routes for ``HttpServer.add_routes``."""
        return self._api.routes()

    async def start(self, ctx: AppContext) -> None:
        """Bind the service to the database."""
        self._service = AgentService(ctx.db, self._events)

    async def stop(self) -> None:
        """Release the service."""
        self._service = None
