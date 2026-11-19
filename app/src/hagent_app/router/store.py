"""SQLite access for the ``invocations`` table (migration ``0600``).

Each function takes a connection. The router calls them through
``Database.run``.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from hagent_app.providers.prices import to_pusd
from hagent_app.router.models import InvocationRequest, InvocationResult
from hagent_app.storage import transaction


def now() -> str:
    """The current time as ISO 8601 text in UTC."""
    return datetime.now(UTC).isoformat()


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


@dataclass(frozen=True, slots=True)
class PendingRow:
    """A queued or running invocation from the last app run."""

    id: str
    request: InvocationRequest
    status: str
    agent_version: int | None
    dry_run: bool


def insert(
    conn: sqlite3.Connection,
    invocation_id: str,
    request: InvocationRequest,
    *,
    status: str,
    dry_run: bool = False,
) -> None:
    """Write a new invocation row."""
    conn.execute(
        "INSERT INTO invocations (id, agent_id, queue_key, source, source_key,"
        " conversation_id, dry_run, request, created_at, status)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            invocation_id,
            request.agent_id,
            request.queue_key,
            request.source,
            request.source_key,
            request.conversation_id,
            int(dry_run),
            request.to_json(),
            now(),
            status,
        ),
    )


def mark_running(
    conn: sqlite3.Connection, invocation_id: str, agent_version: int
) -> None:
    """Set status ``running``, the start time and the definition version."""
    conn.execute(
        "UPDATE invocations SET status = 'running', started_at = ?,"
        " agent_version = ? WHERE id = ?",
        (now(), agent_version, invocation_id),
    )


def finish(conn: sqlite3.Connection, result: InvocationResult) -> None:
    """Write the final state of an invocation."""
    conn.execute(
        "UPDATE invocations SET status = ?, finished_at = ?, path = ?,"
        " fallback_reason = ?, error_code = ?, error = ?, result = ?,"
        " agent_version = COALESCE(?, agent_version), rule_id = ?,"
        " rule_version = ?, turn_id = ?, cost_pusd = ? WHERE id = ?",
        (
            result.status,
            now(),
            result.path,
            result.fallback_reason,
            result.error_code,
            result.error,
            None if result.result is None else _dump(result.result),
            result.agent_version,
            result.rule_id,
            result.rule_version,
            result.turn_id,
            _pusd(result),
            result.invocation_id,
        ),
    )


def _pusd(result: InvocationResult) -> int:
    return to_pusd(result.cost_usd)


def cost_pusd(conn: sqlite3.Connection, invocation_id: str) -> int:
    """The sum of the provider call costs of an invocation."""
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_pusd), 0) FROM provider_calls"
        " WHERE invocation_id = ?",
        (invocation_id,),
    ).fetchone()
    return int(row[0])


def last_result(
    conn: sqlite3.Connection, agent_id: str, source_key: str
) -> tuple[bool, Any]:
    """The result of the last successful invocation from the same source.

    Return ``(found, result)``. Dry runs and fallback results do not count,
    so that a fallback never repeats an earlier fallback.
    """
    row = conn.execute(
        "SELECT result FROM invocations WHERE agent_id = ? AND source_key = ?"
        " AND status = 'ok' AND dry_run = 0"
        " ORDER BY finished_at DESC LIMIT 1",
        (agent_id, source_key),
    ).fetchone()
    if row is None:
        return False, None
    return True, None if row[0] is None else json.loads(row[0])


def pending(conn: sqlite3.Connection) -> list[PendingRow]:
    """Every queued or running invocation, oldest first."""
    rows = conn.execute(
        "SELECT id, request, status, agent_version, dry_run FROM invocations"
        " WHERE status IN ('queued', 'running') ORDER BY created_at, rowid"
    ).fetchall()
    return [
        PendingRow(
            id=row[0],
            request=InvocationRequest.from_json(row[1]),
            status=row[2],
            agent_version=row[3],
            dry_run=bool(row[4]),
        )
        for row in rows
    ]


def get(conn: sqlite3.Connection, invocation_id: str) -> dict[str, Any] | None:
    """One invocation row as a mapping, or ``None``."""
    row = conn.execute(
        "SELECT * FROM invocations WHERE id = ?", (invocation_id,)
    ).fetchone()
    if row is None:
        return None
    data = dict(row)
    data["request"] = json.loads(data["request"])
    data["result"] = None if data["result"] is None else json.loads(data["result"])
    data["dry_run"] = bool(data["dry_run"])
    return data


def insert_finished(
    conn: sqlite3.Connection,
    request: InvocationRequest,
    result: InvocationResult,
) -> None:
    """Write an invocation that ended at once (refused or rejected)."""
    with transaction(conn):
        insert(conn, result.invocation_id, request, status="queued")
        finish(conn, result)
