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
from typing import Protocol

from hagent_app.config import Config
from hagent_app.ha import HAClient, StateCache
from hagent_app.storage import Database, transaction

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
    """The components that a normal app start runs, in start order."""
    return []


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
