"""The daily refresh of the OpenRouter price lists.

OpenRouter publishes its prices at ``GET /api/v1/models`` (chat models) and
``GET /api/v1/embeddings/models``. Both endpoints are public, so the refresh
sends no API key. The refresh stores each model's ``pricing`` object in the
``openrouter_prices`` table and replaces the OpenRouter layer of the price
table.

The refresher checks once an hour and fetches when the stored copy is older
than one day. It waits before the first check, so that a short app start
(and a test) makes no request.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

import httpx2

from hagent_app.providers.meter import Clock, local_now
from hagent_app.providers.prices import PriceTable
from hagent_app.storage import Database, transaction

_LOG = logging.getLogger(__name__)

MAX_AGE = timedelta(days=1)
CHECK_INTERVAL = 3600.0
FIRST_CHECK_DELAY = 60.0
LISTS = (("chat", "/models"), ("embedding", "/embeddings/models"))


def parse_price_list(body: Any) -> dict[str, Mapping[str, Any]]:
    """Return ``{model ID: pricing}`` from an OpenRouter model list."""
    out: dict[str, Mapping[str, Any]] = {}
    data = body.get("data") if isinstance(body, Mapping) else None
    for item in data or []:
        if not isinstance(item, Mapping):
            continue
        model = item.get("id")
        pricing = item.get("pricing")
        if isinstance(model, str) and isinstance(pricing, Mapping):
            out[model] = pricing
    return out


async def load_stored(db: Database) -> tuple[dict[str, Any], str | None]:
    """Return the stored prices and the oldest fetch time."""

    def run(conn: sqlite3.Connection) -> tuple[dict[str, Any], str | None]:
        rows = conn.execute(
            "SELECT model, pricing, fetched_at FROM openrouter_prices"
        ).fetchall()
        prices = {row[0]: json.loads(row[1]) for row in rows}
        oldest = min((row[2] for row in rows), default=None)
        return prices, oldest

    return await db.run(run)


class OpenRouterPriceRefresher:
    """Keeps the OpenRouter layer of the price table current."""

    def __init__(  # noqa: PLR0913 - clock and delay are test seams
        self,
        db: Database,
        prices: PriceTable,
        base_url: str,
        client: httpx2.AsyncClient,
        *,
        clock: Clock = local_now,
        first_delay: float = FIRST_CHECK_DELAY,
    ) -> None:
        self._db = db
        self._prices = prices
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._clock = clock
        self._first_delay = first_delay
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="openrouter-prices")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        await asyncio.sleep(self._first_delay)
        while True:
            try:
                await self.refresh_if_stale()
            except (httpx2.HTTPError, ValueError) as err:
                _LOG.warning("OpenRouter price refresh failed: %s", err)
            await asyncio.sleep(CHECK_INTERVAL)

    async def refresh_if_stale(self) -> bool:
        """Fetch the price lists when the stored copy is too old."""
        _, oldest = await load_stored(self._db)
        if oldest is not None:
            age = self._clock() - datetime.fromisoformat(oldest)
            if age < MAX_AGE:
                return False
        await self.refresh()
        return True

    async def refresh(self) -> int:
        """Fetch both price lists, store them and update the price table."""
        fetched_at = self._clock().isoformat()
        rows: list[tuple[str, str, str, str]] = []
        for kind, path in LISTS:
            response = await self._client.get(f"{self._base_url}{path}")
            response.raise_for_status()
            for model, pricing in parse_price_list(response.json()).items():
                rows.append((model, kind, json.dumps(pricing), fetched_at))
        if not rows:
            msg = "OpenRouter returned empty price lists"
            raise ValueError(msg)

        def run(conn: sqlite3.Connection) -> None:
            with transaction(conn):
                conn.execute("DELETE FROM openrouter_prices")
                conn.executemany(
                    "INSERT OR REPLACE INTO openrouter_prices"
                    " (model, kind, pricing, fetched_at) VALUES (?, ?, ?, ?)",
                    rows,
                )

        await self._db.run(run)
        self._prices.set_openrouter(
            {model: json.loads(pricing) for model, _, pricing, _ in rows}, fetched_at
        )
        _LOG.info("OpenRouter prices refreshed: %d models", len(rows))
        return len(rows)
