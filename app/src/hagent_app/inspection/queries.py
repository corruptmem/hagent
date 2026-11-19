"""Read-only SQL for the inspect routes (design section 7.2, "Inspect").

Each function takes a connection. ``InspectApi`` calls them through
``Database.run``. No function writes. No function reads the ``rules`` or
``rule_versions`` tables: the UI reads rules through the rule routes.

Every payload passes ``redact``, so that an approval action ID or tag from a
tool result or a request never reaches the UI.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from hagent_app.providers.prices import from_pusd
from hagent_app.scheduler.redact import redact

# An invocation row without its large text columns.
_LIST_COLUMNS = (
    "id, agent_id, source, source_key, conversation_id, dry_run, created_at,"
    " started_at, finished_at, status, path, fallback_reason, error_code,"
    " agent_version, rule_id, rule_version, turn_id, cost_pusd"
)
_TOKENS = ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens")


def _load(text: str | None) -> Any:
    return None if text is None else json.loads(text)


def _usd(pusd: int | None) -> float:
    return float(from_pusd(int(pusd or 0)))


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ---------------------------------------------------------------- invocations


@dataclass(frozen=True, slots=True)
class InvocationFilter:
    """The filters of the invocation list. ``None`` means no filter."""

    agent_id: str | None = None
    source: str | None = None
    path: str | None = None
    status: str | None = None
    query: str | None = None
    before: str | None = None
    dry_run: bool = False
    limit: int = 100


def _invocation_brief(row: sqlite3.Row) -> dict[str, Any]:
    data = {k: row[k] for k in row.keys() if k != "cost_pusd"}  # noqa: SIM118
    data["dry_run"] = bool(row["dry_run"])
    data["cost_usd"] = _usd(row["cost_pusd"])
    return data


def list_invocations(
    conn: sqlite3.Connection, filters: InvocationFilter
) -> list[dict[str, Any]]:
    """Invocations, newest first.

    ``query`` is the history search: a case-insensitive text match on the
    request, the result and the error of the invocation, and on the input
    and the result of its agent turns.
    """
    where = ["1 = 1"]
    args: list[Any] = []
    for column, value in (
        ("i.agent_id", filters.agent_id),
        ("i.source", filters.source),
        ("i.path", filters.path),
        ("i.status", filters.status),
    ):
        if value is not None:
            where.append(f"{column} = ?")
            args.append(value)
    if not filters.dry_run:
        where.append("i.dry_run = 0")
    if filters.before is not None:
        where.append("i.created_at < ?")
        args.append(filters.before)
    if filters.query:
        pattern = "%" + _escape_like(filters.query) + "%"
        where.append(
            "(i.request LIKE ? ESCAPE '\\' OR i.result LIKE ? ESCAPE '\\'"
            " OR i.error LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM turns t"
            " WHERE t.invocation_id = i.id AND (t.input LIKE ? ESCAPE '\\'"
            " OR t.result LIKE ? ESCAPE '\\')))"
        )
        args += [pattern] * 5
    columns = ", ".join(f"i.{c.strip()}" for c in _LIST_COLUMNS.split(","))
    sql = (
        f"SELECT {columns} FROM invocations i WHERE {' AND '.join(where)}"  # noqa: S608
        " ORDER BY i.created_at DESC, i.rowid DESC LIMIT ?"
    )
    args.append(filters.limit)
    return [_invocation_brief(r) for r in conn.execute(sql, args)]


def _audit(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "finished_at": row["finished_at"],
        "agent_id": row["agent_id"],
        "agent_version": row["agent_version"],
        "invocation_id": row["invocation_id"],
        "tool": row["tool"],
        "level": row["level"],
        "request": redact(_load(row["request"])),
        "decision": row["decision"],
        "reason_code": row["reason_code"],
        "reason": row["reason"],
        "status": row["status"],
        "error": row["error"],
        "context_id": row["context_id"],
        "previous": redact(_load(row["previous"])),
        "previous_note": row["previous_note"],
    }


def _provider_call(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "provider": row["provider"],
        "call_type": row["call_type"],
        "model": row["model"],
        "status": row["status"],
        "error": row["error"],
        **{k: row[k] for k in _TOKENS},
        "cost_usd": _usd(row["cost_pusd"]),
        "cost_source": row["cost_source"],
    }


def _turn(
    conn: sqlite3.Connection, row: sqlite3.Row, audit: dict[int, dict[str, Any]]
) -> dict[str, Any]:
    calls = []
    for c in conn.execute(
        "SELECT * FROM turn_tool_calls WHERE turn_id = ? ORDER BY seq", (row["id"],)
    ):
        entry = audit.get(c["audit_id"]) if c["audit_id"] is not None else None
        # The executor writes an audit row for each denial and each write.
        # A call without a row passed the scope check.
        calls.append(
            {
                "seq": c["seq"],
                "created_at": c["created_at"],
                "tool": c["tool"],
                "arguments": redact(_load(c["arguments"])),
                "ok": bool(c["ok"]),
                "error_code": c["error_code"],
                "result": redact(_load(c["result"])),
                "result_chars": c["result_chars"],
                "cut": bool(c["cut"]),
                "scope_decision": entry["decision"] if entry else "allowed",
                "audit": entry,
            }
        )
    compactions = [
        {
            "id": c["id"],
            "created_at": c["created_at"],
            "kind": c["kind"],
            "detail": redact(_load(c["detail"])),
            "summary": c["summary"],
        }
        for c in conn.execute(
            "SELECT * FROM turn_compactions WHERE turn_id = ? ORDER BY id",
            (row["id"],),
        )
    ]
    return {
        "id": row["id"],
        "agent_version": row["agent_version"],
        "source": row["source"],
        "source_key": row["source_key"],
        "dry_run": bool(row["dry_run"]),
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "status": row["status"],
        "stop_reason": row["stop_reason"],
        "error": row["error"],
        "input": redact(_load(row["input"])),
        "prompt": redact(row["prompt"]),
        "result": redact(_load(row["result"])),
        "messages": redact(_load(row["messages"])),
        "usage": {
            "requests": row["requests"],
            "tool_calls": row["tool_calls"],
            **{k: row[k] for k in _TOKENS},
        },
        "tool_calls": calls,
        "compactions": compactions,
    }


def invocation_trace(
    conn: sqlite3.Connection, invocation_id: str
) -> dict[str, Any] | None:
    """The full trace of one invocation, or ``None``."""
    row = conn.execute(
        "SELECT * FROM invocations WHERE id = ?", (invocation_id,)
    ).fetchone()
    if row is None:
        return None
    invocation = _invocation_brief(row)
    invocation["request"] = redact(_load(row["request"]))
    invocation["result"] = redact(_load(row["result"]))
    invocation["error"] = row["error"]
    audit_rows = [
        _audit(r)
        for r in conn.execute(
            "SELECT * FROM tool_audit WHERE invocation_id = ? ORDER BY id",
            (invocation_id,),
        )
    ]
    audit = {a["id"]: a for a in audit_rows}
    turns = [
        _turn(conn, r, audit)
        for r in conn.execute(
            "SELECT * FROM turns WHERE invocation_id = ? ORDER BY started_at, rowid",
            (invocation_id,),
        )
    ]
    calls = [
        _provider_call(r)
        for r in conn.execute(
            "SELECT * FROM provider_calls WHERE invocation_id = ? ORDER BY id",
            (invocation_id,),
        )
    ]
    trials = [
        {
            "id": r["id"],
            "rule_id": r["rule_id"],
            "version": r["version"],
            "turn_id": r["turn_id"],
            "status": r["status"],
            "rule_result": redact(_load(r["rule_result"])),
            "agent_result": redact(_load(r["agent_result"])),
            "detail": r["detail"],
            "cost_usd": _usd(r["cost_pusd"]),
        }
        for r in conn.execute(
            "SELECT * FROM rule_trials WHERE invocation_id = ? ORDER BY created_at",
            (invocation_id,),
        )
    ]
    approvals = [
        {"id": r["id"], "tool": r["tool"], "status": r["status"]}
        for r in conn.execute(
            "SELECT id, tool, status FROM approval_requests"
            " WHERE invocation_id = ? ORDER BY created_at",
            (invocation_id,),
        )
    ]
    cost = sum(c["cost_usd"] for c in calls)
    return {
        "invocation": invocation,
        "turns": turns,
        "audit": audit_rows,
        "provider_calls": calls,
        "trials": trials,
        "approvals": approvals,
        "cost": {
            "total_usd": cost,
            **{k: sum(int(c[k]) for c in calls) for k in _TOKENS},
        },
    }


# ---------------------------------------------------------------- audit log


@dataclass(frozen=True, slots=True)
class AuditFilter:
    """The filters of the audit log. ``None`` means no filter."""

    agent_id: str | None = None
    decision: str | None = None
    tool: str | None = None
    before: int | None = None
    limit: int = 100


def list_audit(conn: sqlite3.Connection, filters: AuditFilter) -> list[dict[str, Any]]:
    """Audit rows, newest first."""
    where = ["1 = 1"]
    args: list[Any] = []
    for column, value in (
        ("agent_id", filters.agent_id),
        ("decision", filters.decision),
        ("tool", filters.tool),
    ):
        if value is not None:
            where.append(f"{column} = ?")
            args.append(value)
    if filters.before is not None:
        where.append("id < ?")
        args.append(filters.before)
    sql = (
        f"SELECT * FROM tool_audit WHERE {' AND '.join(where)}"  # noqa: S608
        " ORDER BY id DESC LIMIT ?"
    )
    args.append(filters.limit)
    return [_audit(r) for r in conn.execute(sql, args)]


# ------------------------------------------------- schedules and subscriptions


def schedules(conn: sqlite3.Connection, agent_id: str | None) -> dict[str, Any]:
    """Schedules and subscriptions, each with its last run or event."""
    where, args = ("WHERE agent_id = ?", [agent_id]) if agent_id else ("", [])
    items = []
    for r in conn.execute(
        f"SELECT * FROM scheduler_items {where} ORDER BY agent_id, created_at",  # noqa: S608
        args,
    ):
        last_run = conn.execute(
            "SELECT due_at, at, status, missed_count, invocation_id, result_status,"
            " result_code FROM scheduler_runs WHERE item_id = ?"
            " ORDER BY id DESC LIMIT 1",
            (r["id"],),
        ).fetchone()
        last_event = conn.execute(
            "SELECT event_type, entity_id, origin, received_at, outcome, detail,"
            " invocation_id FROM scheduler_events WHERE subscription_id = ?"
            " ORDER BY id DESC LIMIT 1",
            (r["id"],),
        ).fetchone()
        items.append(
            {
                "id": r["id"],
                "agent_id": r["agent_id"],
                "kind": r["kind"],
                "spec": redact(_load(r["spec"])),
                "enabled": bool(r["enabled"]),
                "run_at": r["run_at"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "last_run": dict(last_run) if last_run else None,
                "last_event": dict(last_event) if last_event else None,
            }
        )
    # The last run of the definition review of each agent. SQLite takes
    # the other columns from the row with the highest ``at``.
    review_sql = (
        "SELECT agent_id, MAX(at) AS at, due_at, status, invocation_id"
        " FROM scheduler_runs WHERE item_id IS NULL"
    )
    if agent_id:
        review_sql += " AND agent_id = ?"
    reviews = [dict(r) for r in conn.execute(review_sql + " GROUP BY agent_id", args)]
    runs = [
        dict(r)
        for r in conn.execute(
            f"SELECT * FROM scheduler_runs {where} ORDER BY id DESC LIMIT 50",  # noqa: S608
            args,
        )
    ]
    events = [
        dict(r)
        for r in conn.execute(
            f"SELECT * FROM scheduler_events {where} ORDER BY id DESC LIMIT 50",  # noqa: S608
            args,
        )
    ]
    return {"items": items, "reviews": reviews, "runs": runs, "events": events}


# ---------------------------------------------------------------- cost


def _totals(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "calls": int(row["calls"] or 0),
        "cost_usd": _usd(row["cost"]),
        "embedding_cost_usd": _usd(row["embedding_cost"]),
        **{k: int(row[k] or 0) for k in _TOKENS},
    }


def cost(
    conn: sqlite3.Connection, since_day: str, agent_id: str | None
) -> dict[str, Any]:
    """Provider cost from ``since_day`` (local date), by agent, day and path.

    The path is the path of the linked invocation. A call without an
    invocation has the path ``embedding`` for an embedding, else ``other``.
    An invocation that did not finish has the path ``none``.
    """
    sums = (
        "COUNT(*) AS calls, SUM(p.cost_pusd) AS cost,"
        " SUM(CASE WHEN p.call_type = 'embedding' THEN p.cost_pusd ELSE 0 END)"
        " AS embedding_cost, " + ", ".join(f"SUM(p.{k}) AS {k}" for k in _TOKENS)
    )
    path = (
        "CASE WHEN p.invocation_id IS NULL THEN"
        " (CASE WHEN p.call_type = 'embedding' THEN 'embedding' ELSE 'other' END)"
        " ELSE COALESCE(i.path, 'none') END"
    )
    base = (
        " FROM provider_calls p LEFT JOIN invocations i ON i.id = p.invocation_id"
        " WHERE p.budget_day >= ?"
    )
    args: list[Any] = [since_day]
    if agent_id is not None:
        base += " AND p.agent_id = ?"
        args.append(agent_id)

    def group(key: str, name: str) -> list[dict[str, Any]]:
        sql = f"SELECT {key} AS k, {sums}{base} GROUP BY k ORDER BY k"
        return [{name: r["k"], **_totals(r)} for r in conn.execute(sql, args)]

    total = conn.execute(f"SELECT {sums}{base}", args).fetchone()
    return {
        "since": since_day,
        "total": _totals(total),
        "by_agent": group("p.agent_id", "agent_id"),
        "by_day": group("p.budget_day", "day"),
        "by_path": group(path, "path"),
        "by_type": group("p.call_type", "call_type"),
    }


def denied_calls(
    conn: sqlite3.Connection, agent_id: str, limit: int
) -> list[dict[str, Any]]:
    """The newest denied calls of one agent."""
    return list_audit(
        conn, AuditFilter(agent_id=agent_id, decision="denied", limit=limit)
    )
