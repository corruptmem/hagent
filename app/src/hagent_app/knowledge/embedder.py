"""The background embedding queue (design section 13.3).

The queue is the set of passages that have no vector for the current
embedding model. The embedder takes them in batches and calls the embedding
provider. It embeds each content hash once per model: passages with the same
text share one vector.

Charges:

- Text that an agent writes counts towards the budget of that agent
  (``charge_agent_id`` of the passage).
- Documents and the entity catalogue count towards the installation only
  (``charge_agent_id`` is ``NULL``).

A change of model changes the model key, so every passage is in the queue
again: a full re-index. The embedder deletes the vectors of other models
and vectors whose text no longer exists.

If the provider fails, the embedder records the reason, waits, and tries
again. Search uses keywords only for passages that have no vector.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from hagent_app.knowledge import vectors
from hagent_app.knowledge.index import SearchIndex, content_hash
from hagent_app.providers.config import PROVIDERS, Provider
from hagent_app.providers.embeddings import EmbeddingClient
from hagent_app.storage import Database, transaction

_LOG = logging.getLogger(__name__)
QUERY_CACHE_SIZE = 256


@dataclass(frozen=True, slots=True)
class EmbeddingModel:
    """The one embedding model of the installation."""

    provider: Provider
    model: str
    dimensions: int | None = None

    @property
    def key(self) -> str:
        """The key of the vectors of this model in ``search_embeddings``."""
        dims = "" if self.dimensions is None else str(self.dimensions)
        return f"{self.provider}:{self.model}:{dims}"

    def to_data(self) -> dict[str, Any]:
        """The JSON form."""
        return {
            "provider": self.provider,
            "model": self.model,
            "dimensions": self.dimensions,
        }


def parse_provider(value: str) -> Provider:
    """A provider name, or ``ValueError``."""
    for provider in PROVIDERS:
        if provider == value:
            return provider
    msg = f"unknown provider {value!r}"
    raise ValueError(msg)


class Embedder:
    """Fills ``search_embeddings`` from a background task."""

    def __init__(
        self,
        db: Database,
        index: SearchIndex,
        client: Callable[[], EmbeddingClient | None],
        *,
        batch_size: int = 64,
        retry_seconds: float = 60.0,
    ) -> None:
        self._db = db
        self._index = index
        self._client = client
        self._batch_size = batch_size
        self._retry = retry_seconds
        self._model: EmbeddingModel | None = None
        self._wake = asyncio.Event()
        self._idle = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._query_cache: dict[tuple[str, str], list[float]] = {}
        self.last_error: str | None = None
        self.embedded_texts = 0

    @property
    def model(self) -> EmbeddingModel | None:
        """The current model, or ``None``."""
        return self._model

    @property
    def active(self) -> bool:
        """True if the embedder can make vectors."""
        return self._model is not None and self._index.vectors_loaded

    def set_model(self, model: EmbeddingModel | None) -> None:
        """Select the model. A change re-indexes every passage."""
        if model != self._model:
            self._model = model
            self._query_cache.clear()
            self.last_error = None
        self._index.embed_query = self.embed_query if self.active else None
        self.wake()

    def start(self) -> None:
        """Start the background task."""
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="knowledge-embedder")

    async def stop(self) -> None:
        """Stop the background task."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def wake(self) -> None:
        """Tell the task that new passages wait."""
        self._idle.clear()
        self._wake.set()

    async def wait_idle(self) -> None:
        """Wait until the queue is empty or the provider failed (for tests)."""
        await self._idle.wait()

    async def embed_query(self, text: str) -> tuple[str, list[float]] | None:
        """The vector of a search query. Charged to the installation."""
        model = self._model
        client = self._client()
        if model is None or client is None or not self._index.vectors_loaded:
            return None
        cache_key = (model.key, content_hash(text))
        cached = self._query_cache.get(cache_key)
        if cached is None:
            result = await client.embed(
                model.provider,
                model.model,
                [text],
                agent_id=None,
                dimensions=model.dimensions,
            )
            cached = result.vectors[0]
            if len(self._query_cache) >= QUERY_CACHE_SIZE:
                self._query_cache.pop(next(iter(self._query_cache)))
            self._query_cache[cache_key] = cached
        return model.key, cached

    async def pending(self) -> int:
        """The number of content hashes with no vector for the model."""
        if self._model is None:
            return 0
        key = self._model.key

        def count(conn: sqlite3.Connection) -> int:
            row = conn.execute(
                "SELECT COUNT(DISTINCT content_hash) FROM search_passages p "
                "WHERE NOT EXISTS (SELECT 1 FROM search_embeddings e "
                "WHERE e.content_hash = p.content_hash AND e.model = ?)",
                (key,),
            ).fetchone()
            return int(row[0])

        return await self._db.run(count)

    async def _loop(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            try:
                await self._drain()
            except asyncio.CancelledError:
                raise
            except Exception as err:
                self.last_error = str(err)
                _LOG.warning("embedding failed, retry in %ss: %s", self._retry, err)
                self._idle.set()
                await asyncio.sleep(self._retry)
                self._wake.set()
                continue
            self._idle.set()

    def _batch_reader(
        self, key: str
    ) -> Callable[
        [sqlite3.Connection], tuple[str | None, list[tuple[str, str]]] | None
    ]:
        size = self._batch_size

        def read(
            conn: sqlite3.Connection,
        ) -> tuple[str | None, list[tuple[str, str]]] | None:
            return _next_batch(conn, key, size)

        return read

    async def _drain(self) -> None:
        """Embed batches until no passage waits. Then delete stale vectors."""
        model = self._model
        if model is None or not self._index.vectors_loaded:
            return
        client = self._client()
        if client is None:
            return
        key = model.key
        while True:
            batch = await self._db.run(self._batch_reader(key))
            if not batch:
                break
            charge, items = batch
            result = await client.embed(
                model.provider,
                model.model,
                [text for _, text in items],
                agent_id=charge,
                dimensions=model.dimensions,
            )
            rows = [
                (h, key, len(v), vectors.serialize(v))
                for (h, _), v in zip(items, result.vectors, strict=True)
            ]
            await self._db.run(_writer(rows))
            self.embedded_texts += len(rows)
            self.last_error = None
        await self._db.run(lambda c: _prune(c, key))


def _writer(
    rows: list[tuple[str, str, int, bytes]],
) -> Callable[[sqlite3.Connection], None]:
    def write(conn: sqlite3.Connection) -> None:
        _store(conn, rows)

    return write


def _next_batch(
    conn: sqlite3.Connection, key: str, size: int
) -> tuple[str | None, list[tuple[str, str]]] | None:
    """The next batch: hashes with no vector, all with the same charge."""
    rows = conn.execute(
        "SELECT p.content_hash, MIN(p.text), MIN(p.charge_agent_id) "
        "FROM search_passages p WHERE NOT EXISTS (SELECT 1 FROM search_embeddings e "
        "WHERE e.content_hash = p.content_hash AND e.model = ?) "
        "GROUP BY p.content_hash ORDER BY MIN(p.id) LIMIT ?",
        (key, size * 4),
    ).fetchall()
    if not rows:
        return None
    charge = rows[0][2]
    items = [(r[0], r[1]) for r in rows if r[2] == charge][:size]
    return charge, items


def _store(conn: sqlite3.Connection, rows: list[tuple[str, str, int, bytes]]) -> None:
    now = datetime.now(UTC).isoformat()
    with transaction(conn):
        conn.executemany(
            "INSERT OR IGNORE INTO search_embeddings "
            "(content_hash, model, dims, vector, created_at) VALUES (?, ?, ?, ?, ?)",
            [(*row, now) for row in rows],
        )


def _prune(conn: sqlite3.Connection, key: str) -> None:
    with transaction(conn):
        conn.execute(
            "DELETE FROM search_embeddings WHERE model != ? OR content_hash NOT IN "
            "(SELECT content_hash FROM search_passages)",
            (key,),
        )
