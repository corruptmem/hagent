"""The tool audit log (design section 8, "Audit").

The executor writes one row for each tool call that changes HA, and one row
for each denied call of any tool. For a change, it writes the row with
status ``pending`` before it calls HA, and it sets the final status after
the call. If the first write fails, the executor does not call HA.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from hagent_app.storage import Database

type Status = Literal["ok", "error", "dry_run"]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One row of the audit log."""

    id: int
    created_at: str
    finished_at: str | None
    agent_id: str
    agent_version: int
    invocation_id: str | None
    tool: str
    level: str
    request: Any
    decision: str
    reason_code: str | None
    reason: str | None
    status: str
    error: str | None
    context_id: str | None
    previous: Any
    previous_note: str | None


@dataclass(frozen=True, slots=True)
class AuditCall:
    """The fields that identify a tool call in the audit log."""

    agent_id: str
    agent_version: int
    invocation_id: str | None
    tool: str
    level: str
    request: Any


class AuditLog:
    """Write and read the audit log."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def denied(self, call: AuditCall, code: str, reason: str) -> int:
        """Record a denied call. Return the row ID."""

        def run(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                "INSERT INTO tool_audit (created_at, finished_at, agent_id,"
                " agent_version, invocation_id, tool, level, request, decision,"
                " reason_code, reason, status)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'denied', ?, ?, 'denied')",
                (
                    _now(),
                    _now(),
                    call.agent_id,
                    call.agent_version,
                    call.invocation_id,
                    call.tool,
                    call.level,
                    _json(call.request),
                    code,
                    reason,
                ),
            )
            return int(cur.lastrowid or 0)

        return await self._db.run(run)

    async def begin(
        self,
        call: AuditCall,
        *,
        previous: Any = None,
        previous_note: str | None = None,
    ) -> int:
        """Record a permitted change before it runs. Return the row ID."""

        def run(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                "INSERT INTO tool_audit (created_at, agent_id, agent_version,"
                " invocation_id, tool, level, request, decision, status,"
                " previous, previous_note)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'allowed', 'pending', ?, ?)",
                (
                    _now(),
                    call.agent_id,
                    call.agent_version,
                    call.invocation_id,
                    call.tool,
                    call.level,
                    _json(call.request),
                    None if previous is None else _json(previous),
                    previous_note,
                ),
            )
            return int(cur.lastrowid or 0)

        return await self._db.run(run)

    async def finish(
        self,
        row_id: int,
        status: Status,
        *,
        error: str | None = None,
        context_id: str | None = None,
    ) -> None:
        """Set the final status of a change."""
        await self._db.run(
            lambda c: c.execute(
                "UPDATE tool_audit SET status = ?, error = ?, context_id = ?,"
                " finished_at = ? WHERE id = ?",
                (status, error, context_id, _now(), row_id),
            )
        )

    async def entries(
        self, agent_id: str | None = None, limit: int = 100
    ) -> list[AuditEntry]:
        """The latest rows, newest first."""

        def run(conn: sqlite3.Connection) -> list[AuditEntry]:
            sql = "SELECT * FROM tool_audit"
            args: list[Any] = []
            if agent_id is not None:
                sql += " WHERE agent_id = ?"
                args.append(agent_id)
            sql += " ORDER BY id DESC LIMIT ?"
            args.append(limit)
            return [_entry(row) for row in conn.execute(sql, args)]

        return await self._db.run(run)


def _entry(row: sqlite3.Row) -> AuditEntry:
    return AuditEntry(
        id=row["id"],
        created_at=row["created_at"],
        finished_at=row["finished_at"],
        agent_id=row["agent_id"],
        agent_version=row["agent_version"],
        invocation_id=row["invocation_id"],
        tool=row["tool"],
        level=row["level"],
        request=json.loads(row["request"]),
        decision=row["decision"],
        reason_code=row["reason_code"],
        reason=row["reason"],
        status=row["status"],
        error=row["error"],
        context_id=row["context_id"],
        previous=None if row["previous"] is None else json.loads(row["previous"]),
        previous_note=row["previous_note"],
    )
