"""The history and conversation index (design section 13.3).

Task-10 writes history. It indexes each record here, and its
``HistoryStore.search`` calls ``search``. ``memory.search`` then finds
records with hybrid ranking. The agent ID comes from the tool executor, and
the search sees the records of that agent only.

Terms:

- **History record**: a short text for one invocation (source, decision,
  result), a review report or a compaction summary.
- **Conversation record**: one Assist message.

Embeddings of these texts count towards the budget of the agent.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from typing import Any, Literal

from hagent_app.knowledge.index import (
    IndexEntry,
    Scope,
    SearchIndex,
    SearchResult,
    delete_source,
    hit_json,
    replace_source,
)
from hagent_app.knowledge.text import Section, split
from hagent_app.storage import Database, transaction

type RecordCollection = Literal["history", "conversation"]


class HistoryIndex:
    """Indexes and searches the history and conversations of each agent."""

    def __init__(
        self,
        db: Database,
        index: SearchIndex,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self._db = db
        self._index = index
        self._on_change = on_change

    async def add(  # noqa: PLR0913
        self,
        agent_id: str,
        record_id: str,
        text: str,
        *,
        collection: RecordCollection = "history",
        source: str | None = None,
        record_type: str | None = None,
        at: str | None = None,
    ) -> None:
        """Index one record. A second call with the same ID replaces it."""
        entry = IndexEntry(
            collection,
            record_id,
            agent_id=agent_id,
            charge_agent_id=agent_id,
            source=source,
            record_type=record_type,
            at=at,
        )
        passages = split([Section(None, None, text)])

        def write(conn: sqlite3.Connection) -> None:
            with transaction(conn):
                replace_source(conn, entry, passages)

        await self._db.run(write)
        if self._on_change is not None:
            self._on_change()

    async def remove(
        self, agent_id: str, record_id: str, collection: RecordCollection = "history"
    ) -> None:
        """Delete one record from the index, for example after retention."""
        entry = IndexEntry(collection, record_id, agent_id=agent_id)

        def write(conn: sqlite3.Connection) -> None:
            with transaction(conn):
                delete_source(conn, entry)

        await self._db.run(write)

    async def search_records(
        self, agent_id: str, query: str, filters: Mapping[str, Any], limit: int
    ) -> SearchResult:
        """Search the records of one agent, with exact filters."""
        record_types = filters.get("record_types")
        collections: tuple[RecordCollection, ...] = ("history", "conversation")
        if filters.get("collection") in {"history", "conversation"}:
            collections = (filters["collection"],)
        scope = Scope(
            collections,
            agent_id=agent_id,
            source=filters.get("source"),
            record_types=tuple(record_types) if record_types else None,
            since=filters.get("since"),
            until=filters.get("until"),
        )
        return await self._index.search(scope, query, limit)

    async def search(
        self, agent_id: str, query: str, filters: Mapping[str, Any], limit: int
    ) -> list[dict[str, Any]]:
        """The ``HistoryStore.search`` form: JSON hits."""
        result = await self.search_records(agent_id, query, filters, limit)
        hits = []
        for hit in result.hits:
            data = hit_json(hit)
            data["record_id"] = data.pop("source_id")
            if result.mode != "hybrid":
                data["search_mode"] = "keyword"
            hits.append(data)
        return hits
