"""The search index of all collections (design section 13.3).

**Hybrid search** runs a keyword search and a vector search on the same
query, and merges the two result lists:

- The keyword search uses FTS5, the full-text index of SQLite.
- The vector search uses sqlite-vec (see ``vectors.py``).
- The merge uses reciprocal rank fusion (RRF). Each result gets
  ``1 / (RRF_K + rank)`` from each list, and the scores add up.

A **collection** is one kind of indexed text: ``knowledge`` (store
passages), ``history``, ``conversation`` or ``entity`` (the HA entity
catalogue). Each search names a scope. The scope filter applies before the
ranking and before the limit, so a passage outside the scope never takes the
place of a passage inside it.

If sqlite-vec does not load, no embedding model is set, or the embedding
call fails, the search uses keywords only. ``SearchResult.mode`` and
``fallback_reason`` say this.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from hagent_app.knowledge import vectors
from hagent_app.knowledge.text import Passage
from hagent_app.storage import Database

_LOG = logging.getLogger(__name__)

RRF_K = 60
# Cosine distance above which a vector match is not a result. The right
# value depends on the embedding model (option max_vector_distance).
DEFAULT_MAX_DISTANCE = 0.75
MAX_QUERY_WORDS = 32
_WORD = re.compile(r"\w+", re.UNICODE)

type Collection = Literal["knowledge", "history", "conversation", "entity"]
# Returns (model key, query vector), or raises when the provider fails.
type QueryEmbedder = Callable[[str], Awaitable[tuple[str, list[float]] | None]]


def content_hash(text: str) -> str:
    """The content hash of a text: SHA-256, hex."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fts_query(query: str) -> str | None:
    """An FTS5 query from free text: each word quoted, joined with OR.

    Model queries can hold ``"``, ``-``, ``:``, ``*`` or ``AND``, which are
    FTS5 syntax. Quoted words are plain terms.
    """
    words = list(dict.fromkeys(_WORD.findall(query.lower())))[:MAX_QUERY_WORDS]
    if not words:
        return None
    return " OR ".join('"' + w.replace('"', '""') + '"' for w in words)


@dataclass(frozen=True, slots=True)
class Scope:
    """The passages that one search can see.

    ``collections`` limits the collection. ``store_ids`` limits knowledge
    passages to stores. ``agent_id`` limits history and conversation
    passages to one agent. ``source_ids`` limits entity passages to the
    entities in an HA scope. A ``None`` limit is not permitted for the
    collection that needs it: the search then finds nothing.
    """

    collections: tuple[Collection, ...]
    store_ids: tuple[str, ...] | None = None
    agent_id: str | None = None
    source_ids: tuple[str, ...] | None = None
    source: str | None = None
    record_types: tuple[str, ...] | None = None
    since: str | None = None
    until: str | None = None

    def where(self) -> tuple[str, list[object]]:
        """A WHERE clause over ``p`` (``search_passages``) and its parameters."""
        clauses = ["p.collection IN (SELECT value FROM json_each(?))"]
        params: list[object] = [json.dumps(list(self.collections))]
        if "knowledge" in self.collections:
            clauses.append(
                "(p.collection != 'knowledge' OR p.store_id IN "
                "(SELECT value FROM json_each(?)))"
            )
            params.append(json.dumps(list(self.store_ids or ())))
        if {"history", "conversation"} & set(self.collections):
            clauses.append(
                "(p.collection NOT IN ('history', 'conversation') OR p.agent_id = ?)"
            )
            params.append(self.agent_id or "")
        if "entity" in self.collections:
            clauses.append(
                "(p.collection != 'entity' OR p.source_id IN "
                "(SELECT value FROM json_each(?)))"
            )
            params.append(json.dumps(list(self.source_ids or ())))
        if self.source is not None:
            clauses.append("p.source = ?")
            params.append(self.source)
        if self.record_types is not None:
            clauses.append("p.record_type IN (SELECT value FROM json_each(?))")
            params.append(json.dumps(list(self.record_types)))
        if self.since is not None:
            clauses.append("p.at >= ?")
            params.append(self.since)
        if self.until is not None:
            clauses.append("p.at <= ?")
            params.append(self.until)
        return " AND ".join(clauses), params


@dataclass(frozen=True, slots=True)
class Hit:
    """One search result."""

    passage_id: int
    collection: str
    store_id: str | None
    agent_id: str | None
    source_id: str
    title: str
    heading: str | None
    page: int | None
    text: str
    score: float
    matched: tuple[str, ...]
    source: str | None = None
    record_type: str | None = None
    at: str | None = None


@dataclass(slots=True)
class SearchResult:
    """The hits of one search, and how the search ran."""

    hits: list[Hit]
    mode: Literal["hybrid", "keyword"]
    fallback_reason: str | None = None


@dataclass(frozen=True, slots=True)
class IndexEntry:
    """The fields of one indexed source, apart from its passages."""

    collection: Collection
    source_id: str
    title: str = ""
    store_id: str | None = None
    agent_id: str | None = None
    charge_agent_id: str | None = None
    source: str | None = None
    record_type: str | None = None
    at: str | None = None


def replace_source(
    conn: sqlite3.Connection, entry: IndexEntry, passages: Sequence[Passage]
) -> int:
    """Replace the passages of one source. Run it inside a transaction."""
    delete_source(conn, entry)
    conn.executemany(
        "INSERT INTO search_passages (collection, store_id, agent_id, source_id, "
        "title, heading, page, ordinal, text, content_hash, charge_agent_id, "
        "source, record_type, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                entry.collection,
                entry.store_id,
                entry.agent_id,
                entry.source_id,
                entry.title,
                p.heading,
                p.page,
                p.ordinal,
                p.text,
                content_hash(p.text),
                entry.charge_agent_id,
                entry.source,
                entry.record_type,
                entry.at,
            )
            for p in passages
            if p.text.strip()
        ],
    )
    return len(passages)


def delete_source(conn: sqlite3.Connection, entry: IndexEntry) -> None:
    """Delete the passages of one source."""
    conn.execute(
        "DELETE FROM search_passages WHERE collection = ? AND source_id = ? "
        "AND store_id IS ? AND agent_id IS ?",
        (entry.collection, entry.source_id, entry.store_id, entry.agent_id),
    )


@dataclass
class SearchIndex:
    """Runs hybrid searches. The embedder sets ``embed_query``."""

    db: Database
    vector_reason: str | None = "sqlite-vec is not loaded"
    embed_query: QueryEmbedder | None = None
    max_distance: float = DEFAULT_MAX_DISTANCE
    last_reason: str | None = field(default=None, init=False)

    @property
    def vectors_loaded(self) -> bool:
        """True if sqlite-vec loaded."""
        return self.vector_reason is None

    async def search(self, scope: Scope, query: str, limit: int) -> SearchResult:
        """Search the passages in ``scope``. Rank them with RRF."""
        depth = max(limit * 4, 20)
        vector: tuple[str, list[float]] | None = None
        reason = self.vector_reason
        if reason is None:
            if self.embed_query is None:
                reason = "no embedding model is set"
            else:
                try:
                    vector = await self.embed_query(query)
                except Exception as err:
                    _LOG.warning("query embedding failed: %s", err)
                    reason = f"the embedding provider failed: {err}"
                if vector is None and reason is None:
                    reason = "no embedding model is set"
        self.last_reason = reason

        def run(conn: sqlite3.Connection) -> list[Hit]:
            return _search(
                conn, scope, query, vector, (depth, limit, self.max_distance)
            )

        hits = await self.db.run(run)
        if vector is None:
            return SearchResult(hits, "keyword", reason)
        return SearchResult(hits, "hybrid")


def _search(
    conn: sqlite3.Connection,
    scope: Scope,
    query: str,
    vector: tuple[str, list[float]] | None,
    limits: tuple[int, int, float],
) -> list[Hit]:
    depth, limit, max_distance = limits
    where, params = scope.where()
    lists: dict[str, list[int]] = {}
    match = fts_query(query)
    if match is not None:
        sql = (
            "SELECT p.id FROM search_fts JOIN search_passages p "  # noqa: S608
            "ON p.id = search_fts.rowid WHERE search_fts MATCH ? AND "
            + where
            + " ORDER BY bm25(search_fts) LIMIT ?"
        )
        rows = conn.execute(sql, [match, *params, depth]).fetchall()
        lists["keyword"] = [int(r[0]) for r in rows]
    if vector is not None:
        model, query_vector = vector
        candidates = (
            "SELECT p.id AS id, p.content_hash AS content_hash "
            "FROM search_passages p WHERE " + where
        )
        try:
            lists["vector"] = vectors.nearest(
                conn, candidates, params, query_vector, model, depth, max_distance
            )
        except sqlite3.Error as err:
            _LOG.warning("vector search failed: %s", err)
    scores: dict[int, float] = {}
    matched: dict[int, list[str]] = {}
    for name, ids in lists.items():
        for rank, passage_id in enumerate(ids, start=1):
            scores[passage_id] = scores.get(passage_id, 0.0) + 1.0 / (RRF_K + rank)
            matched.setdefault(passage_id, []).append(name)
    best = sorted(scores, key=lambda i: (-scores[i], i))[:limit]
    if not best:
        return []
    rows = conn.execute(
        "SELECT id, collection, store_id, agent_id, source_id, title, heading, "
        "page, text, source, record_type, at FROM search_passages "
        "WHERE id IN (SELECT value FROM json_each(?))",
        (json.dumps(best),),
    ).fetchall()
    by_id = {int(r["id"]): r for r in rows}
    return [_hit(by_id[i], scores[i], tuple(matched[i])) for i in best if i in by_id]


def _hit(row: sqlite3.Row, score: float, matched: tuple[str, ...]) -> Hit:
    return Hit(
        passage_id=int(row["id"]),
        collection=row["collection"],
        store_id=row["store_id"],
        agent_id=row["agent_id"],
        source_id=row["source_id"],
        title=row["title"],
        heading=row["heading"],
        page=row["page"],
        text=row["text"],
        score=round(score, 6),
        matched=matched,
        source=row["source"],
        record_type=row["record_type"],
        at=row["at"],
    )


def hit_json(hit: Hit) -> dict[str, Any]:
    """The JSON form of a hit, without empty fields."""
    data: dict[str, Any] = {
        "collection": hit.collection,
        "source_id": hit.source_id,
        "title": hit.title,
        "heading": hit.heading,
        "page": hit.page,
        "text": hit.text,
        "score": hit.score,
        "matched": list(hit.matched),
        "store_id": hit.store_id,
        "source": hit.source,
        "record_type": hit.record_type,
        "at": hit.at,
    }
    return {k: v for k, v in data.items() if v is not None and v != ""}
