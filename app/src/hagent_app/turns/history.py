"""The turn history: records of agent turns, tool calls and compactions.

``TurnHistory`` has two roles:

- It is the write and read interface of the turn history (design section
  13). The turn runner writes. The Inspect screens (task-19) read with
  ``list_turns``, ``get``, ``summary`` and ``recent``.
- It implements the ``HistoryStore`` protocol of the tool executor, so that
  ``memory.search`` can search the history of the invoking agent.

Each read filters by agent ID in SQL. An agent therefore never sees the
history of another agent.

Search: a ``HistoryIndex`` (task-11) can index the turns and serve
``search``. Without an index, ``search`` uses a plain text match.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

from hagent_app.storage import Database, transaction
from hagent_app.tools.base import ToolUnavailableError
from hagent_app.turns.models import (
    CompactionKind,
    CompactionRecord,
    SourceSummary,
    ToolCallRecord,
    TurnRecord,
    TurnStatus,
    TurnUsage,
)

_LOG = logging.getLogger(__name__)

# Turns in these states count as history for the prompt and the summaries.
_FINISHED = ("ok", "stopped", "failed")


def now_iso() -> str:
    """The current UTC time in ISO 8601 form."""
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)


def _load(text: str | None) -> Any:
    return None if text is None else json.loads(text)


class HistoryIndex(Protocol):
    """The search index of the knowledge area (task-11), as task-10 uses it.

    The turn history calls ``index_history`` (or ``index_conversation`` for
    an Assist conversation) after each finished turn, and passes
    ``memory.search`` to ``search_history``.
    """

    async def index_history(self, agent_id: str, record: Mapping[str, Any]) -> None:
        """Index one turn record of the agent."""
        ...

    async def index_conversation(
        self, agent_id: str, conversation_id: str, record: Mapping[str, Any]
    ) -> None:
        """Index one turn record of an Assist conversation."""
        ...

    async def search_history(
        self, agent_id: str, query: str, filters: Mapping[str, Any], limit: int
    ) -> list[dict[str, Any]]:
        """Search the history of one agent."""
        ...


class TurnHistory:
    """Write and read the turn history. Bind it to the database on start."""

    def __init__(self, db: Database | None = None) -> None:
        self._db = db
        self.index: HistoryIndex | None = None

    def bind(self, db: Database | None) -> None:
        """Set the database, or ``None`` on stop."""
        self._db = db

    @property
    def db(self) -> Database:
        """The database. Raise ``ToolUnavailableError`` before start."""
        if self._db is None:
            msg = "the turn history is not running"
            raise ToolUnavailableError(msg)
        return self._db

    # ------------------------------------------------------------ writes

    async def begin(  # noqa: PLR0913
        self,
        *,
        turn_id: str,
        agent_id: str,
        agent_version: int,
        invocation_id: str | None,
        source: str,
        source_key: str,
        conversation_id: str | None,
        dry_run: bool,
        input_data: Any,
        instructions_sha256: str,
        prompt: str,
    ) -> str:
        """Record a turn that starts. Return its start time."""
        started_at = now_iso()
        await self.db.run(
            lambda c: c.execute(
                "INSERT INTO turns (id, agent_id, agent_version, invocation_id,"
                " source, source_key, conversation_id, dry_run, started_at,"
                " status, input, instructions_sha256, prompt)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?)",
                (
                    turn_id,
                    agent_id,
                    agent_version,
                    invocation_id,
                    source,
                    source_key,
                    conversation_id,
                    int(dry_run),
                    started_at,
                    _json(input_data),
                    instructions_sha256,
                    prompt,
                ),
            )
        )
        return started_at

    async def finish(  # noqa: PLR0913
        self,
        turn_id: str,
        *,
        status: TurnStatus,
        stop_reason: str | None,
        error: str | None,
        result: Any,
        messages_json: str | None,
        usage: TurnUsage,
    ) -> None:
        """Set the final status, result, messages and usage of a turn."""
        await self.db.run(
            lambda c: c.execute(
                "UPDATE turns SET finished_at = ?, status = ?, stop_reason = ?,"
                " error = ?, result = ?, messages = ?, requests = ?,"
                " tool_calls = ?, input_tokens = ?, cache_read_tokens = ?,"
                " cache_write_tokens = ?, output_tokens = ? WHERE id = ?",
                (
                    now_iso(),
                    status,
                    stop_reason,
                    error,
                    None if result is None else _json(result),
                    messages_json,
                    usage.requests,
                    usage.tool_calls,
                    usage.input_tokens,
                    usage.cache_read_tokens,
                    usage.cache_write_tokens,
                    usage.output_tokens,
                    turn_id,
                ),
            )
        )

    async def add_tool_call(  # noqa: PLR0913
        self,
        turn_id: str,
        *,
        tool_call_id: str | None,
        tool: str,
        arguments: Any,
        ok: bool,
        error_code: str | None,
        result_text: str,
        cut: bool,
        audit_id: int | None,
    ) -> int:
        """Record one tool call with its full result. Return its number."""

        def run(conn: sqlite3.Connection) -> int:
            with transaction(conn):
                row = conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM turn_tool_calls"
                    " WHERE turn_id = ?",
                    (turn_id,),
                ).fetchone()
                seq = int(row[0])
                conn.execute(
                    "INSERT INTO turn_tool_calls (turn_id, seq, created_at,"
                    " tool_call_id, tool, arguments, ok, error_code, result,"
                    " result_chars, cut, audit_id)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        turn_id,
                        seq,
                        now_iso(),
                        tool_call_id,
                        tool,
                        _json(arguments),
                        int(ok),
                        error_code,
                        result_text,
                        len(result_text),
                        int(cut),
                        audit_id,
                    ),
                )
                return seq

        return await self.db.run(run)

    async def add_compaction(
        self,
        turn_id: str,
        kind: CompactionKind,
        detail: Any,
        summary: str | None = None,
    ) -> None:
        """Record one compaction of a turn."""
        await self.db.run(
            lambda c: c.execute(
                "INSERT INTO turn_compactions (turn_id, created_at, kind, detail,"
                " summary) VALUES (?, ?, ?, ?, ?)",
                (turn_id, now_iso(), kind, _json(detail), summary),
            )
        )

    async def set_summary(
        self,
        agent_id: str,
        source_key: str,
        summary: str,
        covers_until: str,
        turn_count: int,
    ) -> None:
        """Write the running summary of one source."""
        await self.db.run(
            lambda c: c.execute(
                "INSERT INTO source_summaries (agent_id, source_key, summary,"
                " covers_until, turn_count, updated_at) VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (agent_id, source_key) DO UPDATE SET"
                " summary = excluded.summary, covers_until = excluded.covers_until,"
                " turn_count = excluded.turn_count, updated_at = excluded.updated_at",
                (agent_id, source_key, summary, covers_until, turn_count, now_iso()),
            )
        )

    async def index_turn(self, record: TurnRecord) -> None:
        """Give a finished turn to the search index, if there is one."""
        if self.index is None or record.dry_run:
            return
        data = brief(record)
        try:
            if record.conversation_id:
                await self.index.index_conversation(
                    record.agent_id, record.conversation_id, data
                )
            else:
                await self.index.index_history(record.agent_id, data)
        except Exception:
            _LOG.exception("cannot index turn %s", record.id)

    # ------------------------------------------------------------- reads

    async def get(self, turn_id: str) -> TurnRecord | None:
        """One turn with its messages, tool calls and compactions."""

        def run(conn: sqlite3.Connection) -> TurnRecord | None:
            row = conn.execute(
                "SELECT * FROM turns WHERE id = ?", (turn_id,)
            ).fetchone()
            if row is None:
                return None
            calls = tuple(
                _tool_call(r)
                for r in conn.execute(
                    "SELECT * FROM turn_tool_calls WHERE turn_id = ? ORDER BY seq",
                    (turn_id,),
                )
            )
            compactions = tuple(
                _compaction(r)
                for r in conn.execute(
                    "SELECT * FROM turn_compactions WHERE turn_id = ? ORDER BY id",
                    (turn_id,),
                )
            )
            return _turn(row, with_messages=True, calls=calls, compactions=compactions)

        return await self.db.run(run)

    async def list_turns(  # noqa: PLR0913
        self,
        agent_id: str | None = None,
        *,
        source_key: str | None = None,
        status: str | None = None,
        invocation_id: str | None = None,
        before: str | None = None,
        limit: int = 50,
    ) -> list[TurnRecord]:
        """Turns, newest first, without messages, tool calls or compactions."""

        def run(conn: sqlite3.Connection) -> list[TurnRecord]:
            where: list[str] = []
            args: list[Any] = []
            for column, value in (
                ("agent_id", agent_id),
                ("source_key", source_key),
                ("status", status),
                ("invocation_id", invocation_id),
            ):
                if value is not None:
                    where.append(f"{column} = ?")
                    args.append(value)
            if before is not None:
                where.append("started_at < ?")
                args.append(before)
            sql = "SELECT * FROM turns"
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY started_at DESC, rowid DESC LIMIT ?"
            args.append(limit)
            return [_turn(r) for r in conn.execute(sql, args)]

        return await self.db.run(run)

    async def summary(self, agent_id: str, source_key: str) -> SourceSummary | None:
        """The running summary of one source, or ``None``."""

        def run(conn: sqlite3.Connection) -> SourceSummary | None:
            row = conn.execute(
                "SELECT * FROM source_summaries WHERE agent_id = ? AND source_key = ?",
                (agent_id, source_key),
            ).fetchone()
            if row is None:
                return None
            return SourceSummary(
                agent_id=row["agent_id"],
                source_key=row["source_key"],
                summary=row["summary"],
                covers_until=row["covers_until"],
                turn_count=row["turn_count"],
                updated_at=row["updated_at"],
            )

        return await self.db.run(run)

    async def unsummarized(
        self, agent_id: str, source_key: str, after: str | None
    ) -> list[TurnRecord]:
        """Finished turns of a source after ``after``, oldest first.

        Dry runs do not count: a test invocation is not part of the history
        that the agent sees.
        """

        def run(conn: sqlite3.Connection) -> list[TurnRecord]:
            sql = (
                "SELECT * FROM turns WHERE agent_id = ? AND source_key = ?"
                " AND dry_run = 0 AND status IN (?, ?, ?)"
            )
            args: list[Any] = [agent_id, source_key, *_FINISHED]
            if after is not None:
                sql += " AND started_at > ?"
                args.append(after)
            sql += " ORDER BY started_at, rowid"
            return [_turn(r) for r in conn.execute(sql, args)]

        return await self.db.run(run)

    async def recent(
        self, agent_id: str, source_key: str, limit: int
    ) -> tuple[SourceSummary | None, list[TurnRecord]]:
        """The running summary and the latest ``limit`` records after it."""
        summary = await self.summary(agent_id, source_key)
        records = await self.unsummarized(
            agent_id, source_key, summary.covers_until if summary else None
        )
        return summary, records[-limit:] if limit > 0 else []

    # ------------------------------------------------ HistoryStore protocol

    async def search(
        self, agent_id: str, query: str, filters: Mapping[str, Any], limit: int
    ) -> list[dict[str, Any]]:
        """Search the history of one agent (the ``memory.search`` tool)."""
        if self.index is not None:
            return await self.index.search_history(agent_id, query, filters, limit)

        def run(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            sql = (
                "SELECT * FROM turns WHERE agent_id = ? AND dry_run = 0"
                " AND (input LIKE ? ESCAPE '\\' OR result LIKE ? ESCAPE '\\')"
            )
            pattern = "%" + _escape_like(query) + "%"
            args: list[Any] = [agent_id, pattern, pattern]
            if source := filters.get("source"):
                sql += " AND (source = ? OR source_key = ?)"
                args += [source, source]
            if since := filters.get("since"):
                sql += " AND started_at >= ?"
                args.append(since)
            if until := filters.get("until"):
                sql += " AND started_at <= ?"
                args.append(until)
            sql += " ORDER BY started_at DESC LIMIT ?"
            args.append(limit)
            return [brief(_turn(r)) for r in conn.execute(sql, args)]

        return await self.db.run(run)

    async def rule_owner(self, rule_id: str) -> str | None:
        """Rules belong to task-13. Until then, no rule has an owner."""
        return None

    async def rule_stats(self, agent_id: str, rule_id: str) -> dict[str, Any]:
        """Rules belong to task-13. Until then, no statistics exist."""
        return {}


def brief(record: TurnRecord) -> dict[str, Any]:
    """A short JSON form of a turn, for search results and the index."""
    return {
        "turn_id": record.id,
        "invocation_id": record.invocation_id,
        "source": record.source,
        "source_key": record.source_key,
        "started_at": record.started_at,
        "status": record.status,
        "stop_reason": record.stop_reason,
        "input": record.input,
        "result": record.result,
    }


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _turn(
    row: sqlite3.Row,
    *,
    with_messages: bool = False,
    calls: Sequence[ToolCallRecord] = (),
    compactions: Sequence[CompactionRecord] = (),
) -> TurnRecord:
    return TurnRecord(
        id=row["id"],
        agent_id=row["agent_id"],
        agent_version=row["agent_version"],
        invocation_id=row["invocation_id"],
        source=row["source"],
        source_key=row["source_key"],
        conversation_id=row["conversation_id"],
        dry_run=bool(row["dry_run"]),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        status=row["status"],
        stop_reason=row["stop_reason"],
        error=row["error"],
        input=_load(row["input"]),
        instructions_sha256=row["instructions_sha256"],
        prompt=row["prompt"],
        result=_load(row["result"]),
        usage=TurnUsage(
            requests=row["requests"],
            tool_calls=row["tool_calls"],
            input_tokens=row["input_tokens"],
            cache_read_tokens=row["cache_read_tokens"],
            cache_write_tokens=row["cache_write_tokens"],
            output_tokens=row["output_tokens"],
        ),
        messages=_load(row["messages"]) if with_messages else None,
        tool_call_records=tuple(calls),
        compactions=tuple(compactions),
    )


def _tool_call(row: sqlite3.Row) -> ToolCallRecord:
    return ToolCallRecord(
        seq=row["seq"],
        created_at=row["created_at"],
        tool_call_id=row["tool_call_id"],
        tool=row["tool"],
        arguments=_load(row["arguments"]),
        ok=bool(row["ok"]),
        error_code=row["error_code"],
        result=_load(row["result"]),
        result_chars=row["result_chars"],
        cut=bool(row["cut"]),
        audit_id=row["audit_id"],
    )


def _compaction(row: sqlite3.Row) -> CompactionRecord:
    return CompactionRecord(
        id=row["id"],
        turn_id=row["turn_id"],
        created_at=row["created_at"],
        kind=row["kind"],
        detail=_load(row["detail"]),
        summary=row["summary"],
    )
