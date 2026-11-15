"""SQL for the scheduler tables (migration 0800).

Each function takes a ``sqlite3.Connection`` and runs inside ``db.run``.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from hagent_app.storage import transaction

type ItemKind = Literal["schedule", "subscription"]


@dataclass(frozen=True, slots=True)
class Item:
    """One schedule or subscription of an agent."""

    id: str
    agent_id: str
    kind: ItemKind
    spec: dict[str, Any]
    enabled: bool
    run_at: str | None
    created_at: str
    updated_at: str


class LimitError(Exception):
    """The agent has the maximum number of schedules and subscriptions."""


_COLUMNS = "id, agent_id, kind, spec, enabled, run_at, created_at, updated_at"


def _item(row: sqlite3.Row | tuple[Any, ...]) -> Item:
    return Item(
        id=row[0],
        agent_id=row[1],
        kind=row[2],
        spec=json.loads(row[3]),
        enabled=bool(row[4]),
        run_at=row[5],
        created_at=row[6],
        updated_at=row[7],
    )


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True)


def get_item(conn: sqlite3.Connection, item_id: str) -> Item | None:
    """One item, or ``None``."""
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM scheduler_items WHERE id = ?",  # noqa: S608
        (item_id,),
    ).fetchone()
    return None if row is None else _item(row)


def list_items(
    conn: sqlite3.Connection,
    *,
    agent_id: str | None = None,
    kind: ItemKind | None = None,
) -> list[Item]:
    """Items, oldest first, for one agent and one kind if given."""
    sql = f"SELECT {_COLUMNS} FROM scheduler_items WHERE 1 = 1"  # noqa: S608
    params: list[Any] = []
    if agent_id is not None:
        sql += " AND agent_id = ?"
        params.append(agent_id)
    if kind is not None:
        sql += " AND kind = ?"
        params.append(kind)
    sql += " ORDER BY created_at, id"
    return [_item(row) for row in conn.execute(sql, params)]


def insert_item(  # noqa: PLR0913
    conn: sqlite3.Connection,
    *,
    agent_id: str,
    kind: ItemKind,
    spec: Mapping[str, Any],
    run_at: str | None,
    limit: int,
    now: str,
) -> Item:
    """Insert an item. Raise ``LimitError`` if the agent has ``limit`` items.

    The count and the insert run in one transaction, so that parallel
    invocations cannot pass the limit together.
    """
    with transaction(conn):
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM scheduler_items WHERE agent_id = ?", (agent_id,)
        ).fetchone()
        if count >= limit:
            raise LimitError
        item_id = uuid.uuid4().hex
        conn.execute(
            f"INSERT INTO scheduler_items ({_COLUMNS}) "  # noqa: S608
            "VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
            (item_id, agent_id, kind, _json(spec), run_at, now, now),
        )
    return Item(item_id, agent_id, kind, dict(spec), True, run_at, now, now)


def update_item(
    conn: sqlite3.Connection,
    item_id: str,
    *,
    spec: Mapping[str, Any],
    run_at: str | None,
    now: str,
) -> Item | None:
    """Replace the spec of an item. Return the item, or ``None``."""
    conn.execute(
        "UPDATE scheduler_items SET spec = ?, run_at = ?, updated_at = ? WHERE id = ?",
        (_json(spec), run_at, now, item_id),
    )
    return get_item(conn, item_id)


def set_enabled(
    conn: sqlite3.Connection, item_id: str, enabled: bool, now: str
) -> Item | None:
    """Turn an item on or off. Return the item, or ``None``."""
    conn.execute(
        "UPDATE scheduler_items SET enabled = ?, updated_at = ? WHERE id = ?",
        (int(enabled), now, item_id),
    )
    return get_item(conn, item_id)


def delete_item(conn: sqlite3.Connection, item_id: str) -> None:
    """Delete an item."""
    conn.execute("DELETE FROM scheduler_items WHERE id = ?", (item_id,))


def delete_agent_items(conn: sqlite3.Connection, agent_id: str) -> list[str]:
    """Delete every item of an agent. Return their IDs."""
    with transaction(conn):
        ids = [
            row[0]
            for row in conn.execute(
                "SELECT id FROM scheduler_items WHERE agent_id = ?", (agent_id,)
            )
        ]
        conn.execute("DELETE FROM scheduler_items WHERE agent_id = ?", (agent_id,))
    return ids


# ------------------------------------------------------------------- runs


def insert_run(  # noqa: PLR0913
    conn: sqlite3.Connection,
    *,
    agent_id: str,
    item_id: str | None,
    source: str,
    due_at: str,
    at: str,
    status: str,
    missed_count: int = 1,
    invocation_id: str | None = None,
    result_code: str | None = None,
) -> int:
    """Record one due time of a schedule. Return the row ID."""
    cursor = conn.execute(
        "INSERT INTO scheduler_runs (agent_id, item_id, source, due_at, at, "
        "status, missed_count, invocation_id, result_code) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            agent_id,
            item_id,
            source,
            due_at,
            at,
            status,
            missed_count,
            invocation_id,
            result_code,
        ),
    )
    return int(cursor.lastrowid or 0)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    result_status: str,
    result_code: str | None,
) -> None:
    """Record the result of a submitted run.

    A run with an error result before the agent ran (for example
    ``agent_off`` or ``busy``) becomes ``refused``.
    """
    refused = result_status == "error" and result_code is not None
    conn.execute(
        "UPDATE scheduler_runs SET result_status = ?, result_code = ?, "
        "status = CASE WHEN ? THEN 'refused' ELSE status END WHERE id = ?",
        (result_status, result_code, int(refused), run_id),
    )


def last_scheduled_run(conn: sqlite3.Connection, agent_id: str) -> str | None:
    """The time of the last submitted agent schedule run of an agent.

    Runs of the definition review (``item_id IS NULL``) do not count.
    """
    row = conn.execute(
        "SELECT MAX(at) FROM scheduler_runs WHERE agent_id = ? "
        "AND item_id IS NOT NULL AND status = 'submitted'",
        (agent_id,),
    ).fetchone()
    return None if row is None else row[0]


def list_runs(conn: sqlite3.Connection, agent_id: str) -> list[dict[str, Any]]:
    """Every run of an agent, oldest first."""
    rows = conn.execute(
        "SELECT * FROM scheduler_runs WHERE agent_id = ? ORDER BY id", (agent_id,)
    ).fetchall()
    return [dict(row) for row in rows]


# ----------------------------------------------------------------- events


@dataclass(frozen=True, slots=True)
class EventRow:
    """One event that matched a subscription."""

    subscription_id: str
    agent_id: str
    event_type: str
    entity_id: str | None
    context_id: str | None
    origin: str
    origin_agent_id: str | None
    received_at: str
    outcome: str
    detail: str | None = None
    invocation_id: str | None = None


def insert_events(conn: sqlite3.Connection, rows: Iterable[EventRow]) -> None:
    """Record events that matched a subscription."""
    conn.executemany(
        "INSERT INTO scheduler_events (subscription_id, agent_id, event_type, "
        "entity_id, context_id, origin, origin_agent_id, received_at, outcome, "
        "detail, invocation_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                r.subscription_id,
                r.agent_id,
                r.event_type,
                r.entity_id,
                r.context_id,
                r.origin,
                r.origin_agent_id,
                r.received_at,
                r.outcome,
                r.detail,
                r.invocation_id,
            )
            for r in rows
        ],
    )


def list_events(conn: sqlite3.Connection, agent_id: str) -> list[dict[str, Any]]:
    """Every recorded event of an agent, oldest first."""
    rows = conn.execute(
        "SELECT * FROM scheduler_events WHERE agent_id = ? ORDER BY id",
        (agent_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def prune(conn: sqlite3.Connection, before: datetime) -> None:
    """Delete runs and events older than ``before``."""
    cutoff = before.isoformat()
    conn.execute("DELETE FROM scheduler_runs WHERE at < ?", (cutoff,))
    conn.execute("DELETE FROM scheduler_events WHERE received_at < ?", (cutoff,))
