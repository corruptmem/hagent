"""The rule store: rules, immutable versions and statistics per version.

``RuleStore`` has three roles:

- It implements ``AgentStateStore`` for the kind ``rule``, so that the
  ``self`` tools can create, change, turn on and off and delete rules.
- It implements ``rule_owner`` and ``rule_stats`` for ``TurnHistory``, so
  that ``memory.rule_stats`` reads the statistics (design section 10.4).
- It gives the rule stage the active rules of an agent, and records the
  outcome of each rule run.

Every SQL statement that reads or changes a rule for an agent filters by
the agent ID, also after the owner check of the tool.

Versions (design section 10.6):

- ``create`` writes version 1. ``update`` writes the next version. A new
  version starts in the state ``draft`` and becomes the current version.
- ``set_enabled`` sets ``active`` or ``off``. It does not add a version.
- ``rollback`` makes an older version the current version. Its statistics
  continue. The state does not change.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from hagent_app.providers.prices import from_pusd
from hagent_app.storage import Database, transaction
from hagent_app.tools.base import ToolUnavailableError
from hagent_app.tools.stores import StateKind, StoreError

_COUNTERS = ("matched", "answered", "declined", "cache_hits", "errors")


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class ActiveRule:
    """One active rule, as the rule stage reads it."""

    id: str
    version: int
    position: int
    spec: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class RuleRun:
    """The outcome of one rule run, for the statistics."""

    matched: bool = False
    answered: bool = False
    declined: bool = False
    cache_hit: bool = False
    error: str | None = None
    cost_pusd: int = 0


class RuleStore:
    """Rules in SQLite. ``bind`` it to the database before use."""

    def __init__(self) -> None:
        self._db: Database | None = None

    def bind(self, db: Database | None) -> None:
        """Set or clear the database."""
        self._db = db

    def _database(self) -> Database:
        if self._db is None:
            msg = "rules are not available yet"
            raise ToolUnavailableError(msg)
        return self._db

    # ------------------------------------------------------------ the tools

    async def owner(self, kind: StateKind, item_id: str) -> str | None:
        """The agent that owns a rule that is not deleted, or ``None``."""
        if kind != "rule":
            return None

        def run(conn: sqlite3.Connection) -> str | None:
            row = conn.execute(
                "SELECT agent_id FROM rules WHERE id = ? AND deleted_at IS NULL",
                (item_id,),
            ).fetchone()
            return None if row is None else str(row[0])

        return await self._database().run(run)

    async def list_items(self, agent_id: str, kind: StateKind) -> list[dict[str, Any]]:
        """The rules of the agent, in order, with the current spec."""

        def run(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute(
                "SELECT r.*, v.spec FROM rules r JOIN rule_versions v"
                " ON v.rule_id = r.id AND v.version = r.current_version"
                " WHERE r.agent_id = ? AND r.deleted_at IS NULL"
                " ORDER BY r.position",
                (agent_id,),
            ).fetchall()
            return [_rule_data(r) for r in rows]

        return await self._database().run(run)

    async def create(
        self, agent_id: str, kind: StateKind, spec: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Create a rule with version 1 in the state ``draft``, at the end."""
        rule_id = uuid.uuid4().hex
        text = json.dumps(dict(spec), sort_keys=True)

        def run(conn: sqlite3.Connection) -> dict[str, Any]:
            now = _now()
            with transaction(conn):
                row = conn.execute(
                    "SELECT COALESCE(MAX(position), -1) + 1 FROM rules"
                    " WHERE agent_id = ? AND deleted_at IS NULL",
                    (agent_id,),
                ).fetchone()
                conn.execute(
                    "INSERT INTO rules (id, agent_id, position, state,"
                    " current_version, latest_version, created_at, updated_at)"
                    " VALUES (?, ?, ?, 'draft', 1, 1, ?, ?)",
                    (rule_id, agent_id, row[0], now, now),
                )
                _add_version(conn, rule_id, 1, text, now)
            return {"id": rule_id, "version": 1, "state": "draft", "position": row[0]}

        return await self._database().run(run)

    async def update(
        self, agent_id: str, kind: StateKind, item_id: str, spec: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Add a new version. It becomes current, in the state ``draft``."""
        text = json.dumps(dict(spec), sort_keys=True)

        def run(conn: sqlite3.Connection) -> dict[str, Any]:
            now = _now()
            with transaction(conn):
                row = _own(conn, agent_id, item_id)
                version = int(row["latest_version"]) + 1
                _add_version(conn, item_id, version, text, now)
                conn.execute(
                    "UPDATE rules SET latest_version = ?, current_version = ?,"
                    " state = 'draft', updated_at = ? WHERE id = ? AND agent_id = ?",
                    (version, version, now, item_id, agent_id),
                )
            return {"id": item_id, "version": version, "state": "draft"}

        return await self._database().run(run)

    async def set_enabled(
        self, agent_id: str, kind: StateKind, item_id: str, enabled: bool
    ) -> None:
        """Set the state ``active`` or ``off``."""

        def run(conn: sqlite3.Connection) -> None:
            with transaction(conn):
                _own(conn, agent_id, item_id)
                conn.execute(
                    "UPDATE rules SET state = ?, updated_at = ?"
                    " WHERE id = ? AND agent_id = ?",
                    ("active" if enabled else "off", _now(), item_id, agent_id),
                )

        await self._database().run(run)

    async def delete(self, agent_id: str, kind: StateKind, item_id: str) -> None:
        """Delete a rule. Its versions and statistics stay for history."""

        def run(conn: sqlite3.Connection) -> None:
            with transaction(conn):
                _own(conn, agent_id, item_id)
                conn.execute(
                    "UPDATE rules SET deleted_at = ?, state = 'off', updated_at = ?"
                    " WHERE id = ? AND agent_id = ?",
                    (_now(), _now(), item_id, agent_id),
                )

        await self._database().run(run)

    async def reorder(self, agent_id: str, rule_ids: Sequence[str]) -> list[str]:
        """Set the order. ``rule_ids`` must name each rule of the agent once."""

        def run(conn: sqlite3.Connection) -> list[str]:
            with transaction(conn):
                current = [
                    str(r[0])
                    for r in conn.execute(
                        "SELECT id FROM rules WHERE agent_id = ?"
                        " AND deleted_at IS NULL ORDER BY position",
                        (agent_id,),
                    )
                ]
                if len(rule_ids) != len(set(rule_ids)) or set(rule_ids) != set(current):
                    raise StoreError(
                        "invalid_order", "name each of your rules exactly once"
                    )
                now = _now()
                for position, rule_id in enumerate(rule_ids):
                    conn.execute(
                        "UPDATE rules SET position = ?, updated_at = ?"
                        " WHERE id = ? AND agent_id = ?",
                        (position, now, rule_id, agent_id),
                    )
            return list(rule_ids)

        return await self._database().run(run)

    async def rollback(
        self, agent_id: str, rule_id: str, version: int
    ) -> dict[str, Any]:
        """Make an older version the current version."""

        def run(conn: sqlite3.Connection) -> dict[str, Any]:
            with transaction(conn):
                row = _own(conn, agent_id, rule_id)
                found = conn.execute(
                    "SELECT 1 FROM rule_versions WHERE rule_id = ? AND version = ?",
                    (rule_id, version),
                ).fetchone()
                if found is None:
                    raise StoreError("unknown_version", f"no version {version}")
                conn.execute(
                    "UPDATE rules SET current_version = ?, updated_at = ?"
                    " WHERE id = ? AND agent_id = ?",
                    (version, _now(), rule_id, agent_id),
                )
            return {"id": rule_id, "version": version, "state": row["state"]}

        return await self._database().run(run)

    async def get_spec(
        self, agent_id: str, rule_id: str, version: int
    ) -> Mapping[str, Any] | None:
        """The spec of one version of one of the agent's rules."""

        def run(conn: sqlite3.Connection) -> Mapping[str, Any] | None:
            row = conn.execute(
                "SELECT v.spec FROM rule_versions v JOIN rules r ON r.id = v.rule_id"
                " WHERE r.agent_id = ? AND v.rule_id = ? AND v.version = ?",
                (agent_id, rule_id, version),
            ).fetchone()
            return None if row is None else dict(json.loads(row[0]))

        return await self._database().run(run)

    # ------------------------------------------------------------- history

    async def rule_owner(self, rule_id: str) -> str | None:
        """The agent that owns the rule, also after a delete."""

        def run(conn: sqlite3.Connection) -> str | None:
            row = conn.execute(
                "SELECT agent_id FROM rules WHERE id = ?", (rule_id,)
            ).fetchone()
            return None if row is None else str(row[0])

        return await self._database().run(run)

    async def rule_stats(self, agent_id: str, rule_id: str) -> dict[str, Any]:
        """The rule, its versions and the statistics of each version."""

        def run(conn: sqlite3.Connection) -> dict[str, Any]:
            rule = conn.execute(
                "SELECT * FROM rules WHERE id = ? AND agent_id = ?",
                (rule_id, agent_id),
            ).fetchone()
            if rule is None:
                return {}
            versions = conn.execute(
                "SELECT v.version, v.created_at, s.* FROM rule_versions v"
                " LEFT JOIN rule_stats s"
                " ON s.rule_id = v.rule_id AND s.version = v.version"
                " WHERE v.rule_id = ? ORDER BY v.version",
                (rule_id,),
            ).fetchall()
            return {
                "rule_id": rule_id,
                "state": rule["state"],
                "current_version": rule["current_version"],
                "latest_version": rule["latest_version"],
                "deleted": rule["deleted_at"] is not None,
                "versions": [_stats_data(v) for v in versions],
            }

        return await self._database().run(run)

    # ---------------------------------------------------------- rule stage

    async def active_rules(self, agent_id: str) -> list[ActiveRule]:
        """The active rules of the agent, in order, with the current spec."""

        def run(conn: sqlite3.Connection) -> list[ActiveRule]:
            rows = conn.execute(
                "SELECT r.id, r.current_version, r.position, v.spec FROM rules r"
                " JOIN rule_versions v"
                " ON v.rule_id = r.id AND v.version = r.current_version"
                " WHERE r.agent_id = ? AND r.deleted_at IS NULL"
                " AND r.state = 'active' ORDER BY r.position",
                (agent_id,),
            ).fetchall()
            return [
                ActiveRule(r[0], int(r[1]), int(r[2]), json.loads(r[3])) for r in rows
            ]

        return await self._database().run(run)

    async def record(self, rule_id: str, version: int, run_: RuleRun) -> None:
        """Add the outcome of one rule run to the statistics of the version."""
        values = (
            int(run_.matched),
            int(run_.answered),
            int(run_.declined),
            int(run_.cache_hit),
            int(run_.error is not None),
        )

        def run(conn: sqlite3.Connection) -> None:
            now = _now()
            with transaction(conn):
                conn.execute(
                    "INSERT OR IGNORE INTO rule_stats (rule_id, version) VALUES (?, ?)",
                    (rule_id, version),
                )
                sets = ", ".join(f"{c} = {c} + ?" for c in _COUNTERS)
                conn.execute(
                    f"UPDATE rule_stats SET {sets}, cost_pusd = cost_pusd + ?,"  # noqa: S608 - fixed names
                    " last_used_at = ? WHERE rule_id = ? AND version = ?",
                    (*values, run_.cost_pusd, now, rule_id, version),
                )
                if run_.error is not None:
                    conn.execute(
                        "UPDATE rule_stats SET last_error = ?, last_error_at = ?"
                        " WHERE rule_id = ? AND version = ?",
                        (run_.error[:2000], now, rule_id, version),
                    )

        await self._database().run(run)


class AgentStateStores:
    """The ``AgentStateStore`` of the tools: one store for each kind.

    Task-13 supplies the rule store. Task-15 adds schedules and
    subscriptions. A kind without a store is unavailable.
    """

    def __init__(self, stores: Mapping[StateKind, Any] | None = None) -> None:
        self.stores: dict[StateKind, Any] = dict(stores or {})

    def _store(self, kind: StateKind) -> Any:
        store = self.stores.get(kind)
        if store is None:
            msg = f"{kind}s are not available yet"
            raise ToolUnavailableError(msg)
        return store

    async def owner(self, kind: StateKind, item_id: str) -> str | None:
        """The owner of the item, or ``None`` (also for a kind with no store)."""
        store = self.stores.get(kind)
        return None if store is None else await store.owner(kind, item_id)

    async def list_items(self, agent_id: str, kind: StateKind) -> list[dict[str, Any]]:
        """The items of one kind that the agent owns."""
        result: list[dict[str, Any]] = await self._store(kind).list_items(
            agent_id, kind
        )
        return result

    async def create(
        self, agent_id: str, kind: StateKind, spec: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Create an item."""
        result: dict[str, Any] = await self._store(kind).create(agent_id, kind, spec)
        return result

    async def update(
        self, agent_id: str, kind: StateKind, item_id: str, spec: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Change an item."""
        result: dict[str, Any] = await self._store(kind).update(
            agent_id, kind, item_id, spec
        )
        return result

    async def set_enabled(
        self, agent_id: str, kind: StateKind, item_id: str, enabled: bool
    ) -> None:
        """Turn an item on or off."""
        await self._store(kind).set_enabled(agent_id, kind, item_id, enabled)

    async def delete(self, agent_id: str, kind: StateKind, item_id: str) -> None:
        """Delete an item."""
        await self._store(kind).delete(agent_id, kind, item_id)

    async def reorder_rules(self, agent_id: str, rule_ids: Sequence[str]) -> list[str]:
        """Set the order of the agent's rules."""
        result: list[str] = await self._store("rule").reorder(agent_id, rule_ids)
        return result

    async def rollback_rule(
        self, agent_id: str, rule_id: str, version: int
    ) -> dict[str, Any]:
        """Make an older version of the rule the current version."""
        result: dict[str, Any] = await self._store("rule").rollback(
            agent_id, rule_id, version
        )
        return result


def _own(conn: sqlite3.Connection, agent_id: str, rule_id: str) -> sqlite3.Row:
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM rules WHERE id = ? AND agent_id = ? AND deleted_at IS NULL",
        (rule_id, agent_id),
    ).fetchone()
    if row is None:
        raise StoreError("not_owner", "the rule does not belong to the agent")
    return row


def _add_version(
    conn: sqlite3.Connection, rule_id: str, version: int, spec: str, now: str
) -> None:
    conn.execute(
        "INSERT INTO rule_versions (rule_id, version, spec, created_at)"
        " VALUES (?, ?, ?, ?)",
        (rule_id, version, spec, now),
    )
    conn.execute(
        "INSERT INTO rule_stats (rule_id, version) VALUES (?, ?)", (rule_id, version)
    )


def _rule_data(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "position": row["position"],
        "state": row["state"],
        "current_version": row["current_version"],
        "latest_version": row["latest_version"],
        "updated_at": row["updated_at"],
        "spec": json.loads(row["spec"]),
    }


def _stats_data(row: sqlite3.Row) -> dict[str, Any]:
    cost = row["cost_pusd"] or 0
    return {
        "version": row["version"],
        "created_at": row["created_at"],
        **{c: row[c] or 0 for c in _COUNTERS},
        "cost_usd": float(from_pusd(cost)),
        "last_used_at": row["last_used_at"],
        "last_error": row["last_error"],
        "last_error_at": row["last_error_at"],
    }
