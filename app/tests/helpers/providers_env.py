"""A started ``ProvidersComponent`` with a fake provider API, for tests."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from hagent_app.app import AppContext
from hagent_app.config import Config
from hagent_app.ha import HAClient, StateCache
from hagent_app.providers import ProvidersComponent
from hagent_app.storage import Database

from helpers.fake_providers import FakeProviders, provider_options


@dataclass
class ProvidersEnv:
    """The component, its database and the fake provider API."""

    component: ProvidersComponent
    db: Database
    fake: FakeProviders

    @classmethod
    async def start(
        cls,
        data_dir: Path,
        clock: Callable[[], datetime] | None,
        options: dict[str, Any],
    ) -> ProvidersEnv:
        await asyncio.to_thread(data_dir.mkdir, parents=True)
        db = await Database.open(data_dir / "hagent.db")
        fake = FakeProviders()
        config = Config(
            ha_url="ws://unused.invalid/api/websocket",
            ha_token="unused",  # noqa: S106 - not a secret
            data_dir=data_dir,
            options={"providers": provider_options(**options)},
        )
        ha = HAClient(config.ha_url, config.ha_token)  # never started
        ctx = AppContext(config, db, ha, StateCache(ha), "test-installation")
        kwargs: dict[str, Any] = {"http_client": fake.client()}
        if clock is not None:
            kwargs["clock"] = clock
        component = ProvidersComponent(**kwargs)
        await component.start(ctx)
        return cls(component, db, fake)

    async def calls(self) -> list[dict[str, Any]]:
        """All ``provider_calls`` rows as dicts, in insert order."""

        def run(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute("SELECT * FROM provider_calls ORDER BY id").fetchall()
            return [dict(row) for row in rows]

        return await self.db.run(run)

    async def reservations(self) -> int:
        return int(
            await self.db.run(
                lambda c: c.execute(
                    "SELECT COUNT(*) FROM budget_reservations"
                ).fetchone()[0]
            )
        )

    async def close(self) -> None:
        await self.component.stop()
        await self.db.close()
