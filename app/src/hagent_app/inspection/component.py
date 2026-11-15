"""The inspect component: read-only routes and live invocation events.

Plug it in, in ``default_components()``, after the router and before the
HTTP server::

    inspect = InspectComponent(http.events, tools=tools, router=router)
    http.add_routes(inspect.routes())

The component reads the tables of other areas, but it never writes. It
publishes the ``invocations`` change events from the router listeners, so
that the UI lists update live.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hagent_app.agents import AgentReader
from hagent_app.http.errors import ApiError
from hagent_app.inspection.api import InspectApi
from hagent_app.tools.registry import RegistryUnavailableError
from hagent_app.tools.scope import ResolvedScope, agent_scope

if TYPE_CHECKING:
    from aiohttp import web

    from hagent_app.agents import Agent
    from hagent_app.app import AppContext
    from hagent_app.http.events import ChangeEvents
    from hagent_app.router import RouterComponent
    from hagent_app.router.models import InvocationResult
    from hagent_app.storage import Database
    from hagent_app.tools import ToolsComponent


def _scope_view(scope: ResolvedScope, known: list[str]) -> dict[str, Any]:
    if scope.all:
        return {"all": True, "entities": [], "domains": []}
    return {
        "all": False,
        "entities": sorted(scope.entity_set(known)),
        "domains": sorted(scope.domains),
    }


class InspectComponent:
    """Owns the inspect routes. See the module text."""

    name = "inspect"

    def __init__(
        self,
        events: ChangeEvents | None = None,
        *,
        tools: ToolsComponent | None = None,
        router: RouterComponent | None = None,
    ) -> None:
        self._events = events
        self._tools = tools
        self._ctx: AppContext | None = None
        self._agents: AgentReader | None = None
        if router is not None:
            router.add_result_listener(self._on_result)
            router.add_activity_listener(self._on_activity)

    def routes(self) -> list[web.RouteDef]:
        """The routes for ``HttpServer.add_routes``."""
        return InspectApi(self).routes()

    async def start(self, ctx: AppContext) -> None:
        """Keep the context. The component has no background work."""
        self._ctx = ctx
        self._agents = AgentReader(ctx.db)

    async def stop(self) -> None:
        """Forget the context."""
        self._ctx = None
        self._agents = None

    # ------------------------------------------------------------ helpers

    def db(self) -> Database:
        """The database. Answer 503 before start."""
        if self._ctx is None:
            raise ApiError(503, "not_ready", "the inspect area is not running")
        return self._ctx.db

    async def agent(self, agent_id: str) -> Agent:
        """The live agent, or 404."""
        self.db()
        assert self._agents is not None  # noqa: S101 - set with the context
        agent = await self._agents.get(agent_id)
        if agent is None:
            raise ApiError(404, "agent_not_found", "the agent does not exist")
        return agent

    def permissions(self, agent: Agent) -> dict[str, Any]:
        """The effective scopes of one agent, resolved to entity IDs."""
        assert self._ctx is not None  # noqa: S101 - agent() checked it
        definition = agent.definition
        registry = self._tools.registry if self._tools else None
        stale = registry is None or registry.stale
        snapshot = registry.last() if registry is not None else None
        known = sorted(state.entity_id for state in self._ctx.states)
        ha: dict[str, Any] = {
            "level": definition.tools.ha.level,
            "services": list(definition.tools.ha.services),
            "outputs": list(definition.outputs),
        }
        try:
            scope = agent_scope(definition, snapshot)
        except RegistryUnavailableError as err:
            ha["unavailable"] = str(err)
        else:
            ha["read"] = _scope_view(scope.read, known)
            ha["control"] = _scope_view(scope.control, known)
        tools: list[dict[str, str]] = []
        if self._tools is not None:
            try:
                specs = self._tools.executor().tools_for(definition)
            except RuntimeError:
                specs = []
            tools = [{"name": s.name, "group": s.group} for s in specs]
        approval = definition.approval
        return {
            "agent_id": agent.id,
            "version": agent.version,
            "registry_stale": stale,
            "ha": ha,
            "tools": tools,
            "knowledge": [
                {"store_id": k.store_id, "access": k.access}
                for k in definition.knowledge
            ],
            "approval": {
                "tools": list(approval.tools),
                "services": list(approval.services),
                "entities": list(approval.entities),
            },
        }

    # ------------------------------------------------------------ events

    def _on_result(self, result: InvocationResult) -> None:
        if self._events is None:
            return
        self._events.publish(
            "invocations",
            "finished",
            result.invocation_id,
            {
                "agent_id": result.agent_id,
                "status": result.status,
                "path": result.path,
            },
        )

    def _on_activity(self, agent_id: str, running: int) -> None:
        if self._events is not None:
            self._events.publish(
                "invocations", "activity", agent_id, {"running": running}
            )
