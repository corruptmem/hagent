"""The history and conversation index (design section 13.3).

Task-10 writes history. ``HistoryIndex`` implements the ``HistoryIndex``
protocol of ``turns/history.py``: the turn history calls ``index_history``
or ``index_conversation`` after each finished turn, and its
``HistoryStore.search`` calls ``search_history``. ``memory.search`` then
finds records with hybrid ranking. The agent ID comes from the tool
executor, and the search sees the records of that agent only.

Terms:

- **History record**: a short text for one invocation (source, decision,
  result), a review report or a compaction summary.
- **Conversation record**: one Assist message.

Embeddings of these texts count towards the budget of the agent.
"""

from __future__ import annotations

import json
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
        source_key: str | None = None,
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
            source_key=source_key,
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

    # ------------------------------------- the protocol of turns/history.py

    async def index_history(self, agent_id: str, record: Mapping[str, Any]) -> None:
        """Index one finished turn (the ``brief`` form of task-10)."""
        await self._index_turn(agent_id, record, "history", "turn")

    async def index_conversation(
        self, agent_id: str, conversation_id: str, record: Mapping[str, Any]
    ) -> None:
        """Index one turn of an Assist conversation."""
        await self._index_turn(
            agent_id, record, "conversation", "message", conversation_id
        )

    async def search_history(
        self, agent_id: str, query: str, filters: Mapping[str, Any], limit: int
    ) -> list[dict[str, Any]]:
        """Search the turns and conversations of one agent (``memory.search``)."""
        hits = await self.search(agent_id, query, filters, limit)
        for hit in hits:
            hit["turn_id"] = hit["record_id"]
        return hits

    async def _index_turn(
        self,
        agent_id: str,
        record: Mapping[str, Any],
        collection: RecordCollection,
        record_type: str,
        conversation_id: str | None = None,
    ) -> None:
        await self.add(
            agent_id,
            str(record["turn_id"]),
            turn_text(record, conversation_id),
            collection=collection,
            source=_opt(record.get("source")),
            source_key=_opt(record.get("source_key")),
            record_type=record_type,
            at=_opt(record.get("started_at")),
        )

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
        return await self._index.search(scope, query, limit, charge_agent_id=agent_id)

    async def search(
        self, agent_id: str, query: str, filters: Mapping[str, Any], limit: int
    ) -> list[dict[str, Any]]:
        """The ``HistoryStore.search`` form: JSON hits."""
        result = await self.search_records(agent_id, query, filters, limit)
        hits = []
        for hit in result.hits:
            data = hit_json(hit)
            data["record_id"] = data.pop("source_id")
            data.pop("title", None)
            if result.mode != "hybrid":
                data["search_mode"] = "keyword"
            hits.append(data)
        return hits


def _opt(value: Any) -> str | None:
    return None if value is None else str(value)


def _short(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def turn_text(record: Mapping[str, Any], conversation_id: str | None = None) -> str:
    """The search text of one turn: source, inputs, data, status and result."""
    lines = [f"source: {record.get('source') or ''}"]
    if conversation_id:
        lines.append(f"conversation: {conversation_id}")
    raw = record.get("input")
    data: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    if data.get("inputs"):
        lines.append("inputs: " + _short(data["inputs"]))
    if data.get("data"):
        lines.append("data: " + _short(data["data"]))
    status = record.get("status")
    if status:
        stop = record.get("stop_reason")
        lines.append(f"status: {status}" + (f" ({stop})" if stop else ""))
    if record.get("result") is not None:
        lines.append("result: " + _short(record["result"]))
    return "\n".join(lines)
