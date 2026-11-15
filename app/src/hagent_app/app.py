"""The composition root of the app.

``App`` opens the database, starts the HA client and the state cache, and
then starts each component. A component is one area of the app, for example
the management API or the scheduler. It implements ``Component``.

To add a component, add it to ``default_components``. See ``app/README.md``.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from hagent_app.agents import AgentsComponent
from hagent_app.approvals import ApprovalsComponent
from hagent_app.config import Config
from hagent_app.ha import HAClient, StateCache
from hagent_app.ha_catalog import CatalogComponent
from hagent_app.http import HttpServer
from hagent_app.ingress import IngressGuard, UiComponent
from hagent_app.storage import Database, transaction
from hagent_app.tools import ToolsComponent

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AppContext:
    """The shared services that each component receives on start."""

    config: Config
    db: Database
    ha: HAClient
    states: StateCache
    installation_id: str


class Component(Protocol):
    """One area of the app with its own start and stop."""

    name: str

    async def start(self, ctx: AppContext) -> None:
        """Start the component. Return when it is ready."""

    async def stop(self) -> None:
        """Stop the component and release its resources."""


def default_components() -> list[Component]:
    """The components that a normal app start runs, in start order.

    Build ``HttpServer`` first, so that each component can add its routes
    before the server starts. Keep ``http`` last in the list: ``App`` then
    starts it after the components that serve its routes, and stops it
    before them.

    ``IngressGuard`` checks the source address of every request (design
    section 6.1), except on the integration link path. ``UiComponent``
    serves the UI files on every path that does not start with ``api/``.
    """
    from hagent_app.knowledge import KnowledgeComponent  # noqa: PLC0415 - import cycle
    from hagent_app.link import LinkComponent  # noqa: PLC0415 - import cycle
    from hagent_app.providers import ProvidersComponent  # noqa: PLC0415 - import cycle
    from hagent_app.router import (  # noqa: PLC0415 - import cycle
        RetryingChatModels,
        RouterComponent,
    )
    from hagent_app.turns import TurnsComponent  # noqa: PLC0415 - import cycle

    http = HttpServer()
    # The integration link connects from HA Core, not through Ingress. The
    # guard exempts only the link path; the link checks the shared secret.
    guard = IngressGuard(exempt_prefixes=LinkComponent.exempt_prefixes)
    http.add_middleware(guard.middleware)
    agents = AgentsComponent(http.events)
    http.add_routes(agents.routes())
    providers = ProvidersComponent()
    approvals = ApprovalsComponent(http.events)
    http.add_routes(approvals.routes())
    tools = ToolsComponent(http.events)
    tools.approval = approvals.gate
    knowledge = KnowledgeComponent(http.events, providers=providers, tools=tools)
    tools.knowledge = knowledge.tools_store
    tools.entities = knowledge.entity_search
    http.add_routes(knowledge.routes())
    # Chat models retry a failed request with backoff (design section 18).
    models = RetryingChatModels(
        lambda ref, agent_id: providers.chat_model(
            ref.provider, ref.model, agent_id=agent_id
        )
    )
    # TurnsComponent plugs its history into tools.history.
    turns = TurnsComponent(providers, tools, model_for=models)
    turns.history.index = knowledge.history_index
    turns.knowledge_context = knowledge.turn_context
    router = RouterComponent(turns, tools=tools, models=models)
    http.add_routes(router.routes())

    async def agent_cost(agent_id: str) -> tuple[Decimal, Decimal]:
        state = await providers.meter.budget_state(agent_id)
        return state.spent_usd, state.remaining_usd

    link = LinkComponent(http.events, router, cost=agent_cost)
    http.add_routes(link.routes())
    catalog = CatalogComponent()
    http.add_routes(catalog.routes())
    ui = UiComponent()
    http.add_routes(ui.routes())
    return [
        providers,
        guard,
        agents,
        approvals,
        tools,
        knowledge,
        turns,
        router,
        link,
        catalog,
        ui,
        http,
    ]


class App:
    """The hagent app."""

    def __init__(
        self,
        config: Config,
        components: Sequence[Component] | None = None,
        *,
        client: HAClient | None = None,
    ) -> None:
        self.config = config
        self._components = (
            list(components) if components is not None else default_components()
        )
        self._client = client
        self._started: list[Component] = []
        self.ctx: AppContext | None = None

    async def start(self) -> AppContext:
        """Start the app. The HA connection continues in the background."""
        self.config.data_dir.mkdir(parents=True, exist_ok=True)
        db = await Database.open(self.config.database_path)
        installation_id = await db.run(_installation_id)
        ha = self._client or HAClient(self.config.ha_url, self.config.ha_token)
        states = StateCache(ha)
        await states.start()
        await ha.start()
        self.ctx = AppContext(self.config, db, ha, states, installation_id)
        _LOG.info("hagent installation %s started", installation_id)
        for component in self._components:
            await component.start(self.ctx)
            self._started.append(component)
        return self.ctx

    async def stop(self) -> None:
        """Stop the components in reverse order, then the core services."""
        while self._started:
            component = self._started.pop()
            try:
                await component.stop()
            except Exception:
                _LOG.exception("component %s failed to stop", component.name)
        if self.ctx is not None:
            await self.ctx.states.stop()
            await self.ctx.ha.stop()
            await self.ctx.db.close()
            self.ctx = None

    async def run(self, stop: asyncio.Event) -> None:
        """Start the app, wait for ``stop``, then stop the app."""
        try:
            await self.start()
            await stop.wait()
        finally:
            await self.stop()


def _installation_id(conn: sqlite3.Connection) -> str:
    """Return the installation ID. Create it on the first start."""
    with transaction(conn):
        row = conn.execute(
            "SELECT value FROM app_info WHERE key = 'installation_id'"
        ).fetchone()
        if row is not None:
            return str(row[0])
        value = uuid.uuid4().hex
        conn.executemany(
            "INSERT INTO app_info (key, value) VALUES (?, ?)",
            [
                ("installation_id", value),
                ("created_at", datetime.now(UTC).isoformat()),
            ],
        )
        return value
