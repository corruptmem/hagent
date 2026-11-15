"""Every sqlite-vec statement of the app (design section 13.3).

sqlite-vec is a SQLite extension for vector search. It is before version
1.0, so its SQL interface can change. This module holds all statements that
use it, and the app pins its version.

The app stores each vector as a float32 BLOB in ``search_embeddings``. A
query computes the cosine distance with ``vec_distance_cosine`` over the
passages that the scope permits. The app does not use ``vec0`` KNN tables:
a KNN query applies its limit before other filters, so a scope filter can
remove every result. At the size of a home installation, a full scan is
fast enough.
"""

from __future__ import annotations

import logging
import sqlite3
import struct
from collections.abc import Sequence

_LOG = logging.getLogger(__name__)


def load(conn: sqlite3.Connection) -> str | None:
    """Load sqlite-vec into ``conn``. Return ``None``, or the reason it failed."""
    try:
        import sqlite_vec  # type: ignore[import-untyped]  # noqa: PLC0415

        conn.enable_load_extension(True)
        try:
            sqlite_vec.load(conn)
        finally:
            conn.enable_load_extension(False)
        conn.execute("SELECT vec_version()").fetchone()
    except (ImportError, AttributeError, sqlite3.Error, OSError) as err:
        _LOG.warning("sqlite-vec is not available: %s", err)
        return f"sqlite-vec does not load: {err}"
    return None


def serialize(vector: Sequence[float]) -> bytes:
    """The float32 BLOB form of a vector."""
    return struct.pack(f"{len(vector)}f", *vector)


def nearest(  # noqa: PLR0913, PLR0917
    conn: sqlite3.Connection,
    candidates_sql: str,
    params: Sequence[object],
    query: Sequence[float],
    model: str,
    limit: int,
    max_distance: float,
) -> list[int]:
    """Passage IDs from ``candidates_sql``, nearest to ``query`` first.

    ``candidates_sql`` selects ``id`` and ``content_hash`` of the passages
    that the scope permits. The limit applies after the scope filter.
    Passages with a cosine distance above ``max_distance`` are not
    results: without this cutoff, every passage in scope would rank.
    """
    sql = (
        "SELECT id FROM (SELECT c.id AS id, "  # noqa: S608 - fixed SQL
        "vec_distance_cosine(e.vector, ?) AS distance FROM ("
        + candidates_sql
        + ") AS c JOIN search_embeddings e ON e.content_hash = c.content_hash "
        "AND e.model = ? AND e.dims = ?) WHERE distance <= ? "
        "ORDER BY distance, id LIMIT ?"
    )
    rows = conn.execute(
        sql,
        [serialize(query), *params, model, len(query), max_distance, limit],
    ).fetchall()
    return [int(row[0]) for row in rows]
