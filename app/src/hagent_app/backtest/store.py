"""SQLite access for backtests and trials (migration ``0750``).

Each function takes a connection. Callers run them through ``Database.run``.
Every read for an agent filters by the agent ID.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from hagent_app.providers.prices import from_pusd
from hagent_app.storage import transaction

COUNTERS = (
    "candidates",
    "matched",
    "no_match",
    "compared",
    "agreed",
    "disagreed",
    "declined",
    "errors",
    "skipped",
    "live_decisions",
    "replayed_decisions",
)


def now() -> str:
    """The current time as ISO 8601 text in UTC."""
    return datetime.now(UTC).isoformat()


def _dump(value: Any) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False, default=str)


def _load(text: str | None) -> Any:
    return None if text is None else json.loads(text)


@dataclass(slots=True)
class Case:
    """The outcome of one past invocation in a backtest.

    ``outcome`` is ``agreed``, ``disagreed``, ``no_match``, ``declined``,
    ``error`` or ``skipped``. ``decision`` is ``replayed`` or ``live`` for a
    decision action, else ``None``. ``writes`` lists the service calls that
    the rule would make. The backtest records them, and never sends them.
    ``matched`` is true when the match template rendered true.
    """

    invocation_id: str
    invocation_at: str
    outcome: str
    matched: bool = False
    decision: str | None = None
    rule_result: Any = None
    agent_result: Any = None
    writes: list[dict[str, Any]] = field(default_factory=list)
    detail: str | None = None


@dataclass(slots=True)
class Totals:
    """The counters of one backtest."""

    candidates: int = 0
    matched: int = 0
    no_match: int = 0
    compared: int = 0
    agreed: int = 0
    disagreed: int = 0
    declined: int = 0
    errors: int = 0
    skipped: int = 0
    live_decisions: int = 0
    replayed_decisions: int = 0

    def add(self, case: Case) -> None:
        """Count one case."""
        self.candidates += 1
        if case.matched:
            self.matched += 1
        counter = {
            "no_match": "no_match",
            "agreed": "agreed",
            "disagreed": "disagreed",
            "declined": "declined",
            "error": "errors",
            "skipped": "skipped",
        }[case.outcome]
        setattr(self, counter, getattr(self, counter) + 1)
        if case.outcome in {"agreed", "disagreed"}:
            self.compared += 1
        if case.decision == "live":
            self.live_decisions += 1
        if case.decision == "replayed":
            self.replayed_decisions += 1

    @property
    def agreement_rate(self) -> float | None:
        """Agreed cases divided by compared cases, or ``None``."""
        return None if self.compared == 0 else self.agreed / self.compared


def insert_backtest(  # noqa: PLR0913
    conn: sqlite3.Connection,
    backtest_id: str,
    *,
    agent_id: str,
    rule_id: str,
    version: int,
    agent_version: int,
    since: str,
    until: str,
    max_cases: int | None,
) -> None:
    """Write a backtest that starts."""
    conn.execute(
        "INSERT INTO rule_backtests (id, agent_id, rule_id, version,"
        " agent_version, status, started_at, since, until, max_cases)"
        " VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?)",
        (
            backtest_id,
            agent_id,
            rule_id,
            version,
            agent_version,
            now(),
            since,
            until,
            max_cases,
        ),
    )


def finish_backtest(  # noqa: PLR0913
    conn: sqlite3.Connection,
    backtest_id: str,
    *,
    status: str,
    totals: Totals,
    cases: Sequence[Case],
    cost_pusd: int,
    error: str | None = None,
) -> None:
    """Write the cases and the final counters of a backtest."""
    with transaction(conn):
        conn.executemany(
            "INSERT INTO rule_backtest_cases (backtest_id, seq, invocation_id,"
            " invocation_at, outcome, decision, rule_result, agent_result,"
            " writes, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    backtest_id,
                    seq,
                    c.invocation_id,
                    c.invocation_at,
                    c.outcome,
                    c.decision,
                    _dump(c.rule_result),
                    _dump(c.agent_result),
                    _dump(c.writes) if c.writes else None,
                    None if c.detail is None else c.detail[:2000],
                )
                for seq, c in enumerate(cases, start=1)
            ],
        )
        sets = ", ".join(f"{name} = ?" for name in COUNTERS)
        conn.execute(
            f"UPDATE rule_backtests SET status = ?, finished_at = ?, {sets},"  # noqa: S608 - fixed names
            " cost_pusd = ?, error = ? WHERE id = ?",
            (
                status,
                now(),
                *(getattr(totals, name) for name in COUNTERS),
                cost_pusd,
                error,
                backtest_id,
            ),
        )


def backtest_json(row: Mapping[str, Any]) -> dict[str, Any]:
    """The JSON form of one backtest row."""
    compared = int(row["compared"])
    return {
        "id": row["id"],
        "rule_id": row["rule_id"],
        "version": row["version"],
        "agent_version": row["agent_version"],
        "status": row["status"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "since": row["since"],
        "until": row["until"],
        "max_cases": row["max_cases"],
        **{name: row[name] for name in COUNTERS},
        "agreement_rate": None if compared == 0 else row["agreed"] / compared,
        "cost_usd": float(from_pusd(int(row["cost_pusd"]))),
        "error": row["error"],
    }


def case_json(row: Mapping[str, Any]) -> dict[str, Any]:
    """The JSON form of one backtest case row."""
    return {
        "seq": row["seq"],
        "invocation_id": row["invocation_id"],
        "invocation_at": row["invocation_at"],
        "outcome": row["outcome"],
        "decision": row["decision"],
        "rule_result": _load(row["rule_result"]),
        "agent_result": _load(row["agent_result"]),
        "writes": _load(row["writes"]) or [],
        "detail": row["detail"],
    }


def list_backtests(
    conn: sqlite3.Connection, agent_id: str, rule_id: str, limit: int = 50
) -> list[dict[str, Any]]:
    """The backtests of one rule of the agent, newest first."""
    rows = conn.execute(
        "SELECT * FROM rule_backtests WHERE agent_id = ? AND rule_id = ?"
        " ORDER BY started_at DESC, rowid DESC LIMIT ?",
        (agent_id, rule_id, limit),
    ).fetchall()
    return [backtest_json(r) for r in rows]


def get_backtest(
    conn: sqlite3.Connection, agent_id: str, backtest_id: str
) -> dict[str, Any] | None:
    """One backtest of the agent with its cases, or ``None``."""
    row = conn.execute(
        "SELECT * FROM rule_backtests WHERE id = ? AND agent_id = ?",
        (backtest_id, agent_id),
    ).fetchone()
    if row is None:
        return None
    data = backtest_json(row)
    cases = conn.execute(
        "SELECT * FROM rule_backtest_cases WHERE backtest_id = ? ORDER BY seq",
        (backtest_id,),
    ).fetchall()
    data["cases"] = [case_json(c) for c in cases]
    return data


# ------------------------------------------------------------------ trials


def insert_trial(  # noqa: PLR0913
    conn: sqlite3.Connection,
    trial_id: str,
    *,
    agent_id: str,
    rule_id: str,
    version: int,
    invocation_id: str,
    rule_result: Any,
) -> None:
    """Write a trial that starts."""
    conn.execute(
        "INSERT INTO rule_trials (id, agent_id, rule_id, version,"
        " invocation_id, created_at, status, rule_result)"
        " VALUES (?, ?, ?, ?, ?, ?, 'running', ?)",
        (
            trial_id,
            agent_id,
            rule_id,
            version,
            invocation_id,
            now(),
            _dump(rule_result),
        ),
    )


def finish_trial(  # noqa: PLR0913
    conn: sqlite3.Connection,
    trial_id: str,
    *,
    status: str,
    turn_id: str | None,
    agent_result: Any,
    detail: str | None,
    cost_pusd: int,
) -> None:
    """Write the end of a trial."""
    conn.execute(
        "UPDATE rule_trials SET status = ?, finished_at = ?, turn_id = ?,"
        " agent_result = ?, detail = ?, cost_pusd = ? WHERE id = ?",
        (
            status,
            now(),
            turn_id,
            _dump(agent_result),
            None if detail is None else detail[:2000],
            cost_pusd,
            trial_id,
        ),
    )


def trial_json(row: Mapping[str, Any]) -> dict[str, Any]:
    """The JSON form of one trial row."""
    return {
        "id": row["id"],
        "rule_id": row["rule_id"],
        "version": row["version"],
        "invocation_id": row["invocation_id"],
        "turn_id": row["turn_id"],
        "created_at": row["created_at"],
        "finished_at": row["finished_at"],
        "status": row["status"],
        "rule_result": _load(row["rule_result"]),
        "agent_result": _load(row["agent_result"]),
        "detail": row["detail"],
        "cost_usd": float(from_pusd(int(row["cost_pusd"]))),
    }


def list_trials(
    conn: sqlite3.Connection, agent_id: str, rule_id: str, limit: int = 50
) -> list[dict[str, Any]]:
    """The trials of one rule of the agent, newest first."""
    rows = conn.execute(
        "SELECT * FROM rule_trials WHERE agent_id = ? AND rule_id = ?"
        " ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (agent_id, rule_id, limit),
    ).fetchall()
    return [trial_json(r) for r in rows]


def close_running(conn: sqlite3.Connection) -> None:
    """Mark backtests and trials of an earlier app run as cancelled."""
    with transaction(conn):
        conn.execute(
            "UPDATE rule_backtests SET status = 'cancelled', finished_at = ?,"
            " error = 'the app stopped' WHERE status = 'running'",
            (now(),),
        )
        conn.execute(
            "UPDATE rule_trials SET status = 'cancelled', finished_at = ?,"
            " detail = 'the app stopped' WHERE status = 'running'",
            (now(),),
        )


# ---------------------------------------------------------- rule statistics


def version_stats(conn: sqlite3.Connection, rule_id: str) -> dict[int, dict[str, Any]]:
    """Backtest and trial figures for each version of one rule.

    ``RuleStore.rule_stats`` adds them to the statistics of each version
    (design section 10.4).
    """
    found: dict[int, dict[str, Any]] = {
        int(v): _empty()
        for (v,) in conn.execute(
            "SELECT version FROM rule_versions WHERE rule_id = ?", (rule_id,)
        )
    }
    rows = conn.execute(
        "SELECT * FROM rule_backtests WHERE rule_id = ? AND status != 'running'"
        " ORDER BY started_at, rowid",
        (rule_id,),
    ).fetchall()
    for row in rows:
        entry = found.setdefault(int(row["version"]), _empty())
        entry["backtests"] += 1
        entry["backtest_cost_pusd"] += int(row["cost_pusd"])
        entry["last_backtest"] = backtest_json(row)
    trials = conn.execute(
        "SELECT version, status, COUNT(*), SUM(cost_pusd) FROM rule_trials"
        " WHERE rule_id = ? GROUP BY version, status",
        (rule_id,),
    ).fetchall()
    for version, status, count, cost in trials:
        entry = found.setdefault(int(version), _empty())
        entry["trials"][status] = entry["trials"].get(status, 0) + int(count)
        entry["trial_cost_pusd"] += int(cost or 0)
    return {v: _finish(e) for v, e in found.items()}


def _empty() -> dict[str, Any]:
    return {
        "backtests": 0,
        "backtest_cost_pusd": 0,
        "last_backtest": None,
        "trials": {},
        "trial_cost_pusd": 0,
    }


def _finish(entry: dict[str, Any]) -> dict[str, Any]:
    trials: dict[str, int] = entry["trials"]
    agreed = trials.get("agreed", 0)
    compared = agreed + trials.get("disagreed", 0)
    last = entry["last_backtest"]
    return {
        "backtest": {
            "runs": entry["backtests"],
            "cost_usd": float(from_pusd(entry["backtest_cost_pusd"])),
            "agreement_rate": None if last is None else last["agreement_rate"],
            "last": last,
        },
        "trial": {
            "runs": sum(trials.values()),
            "agreed": agreed,
            "disagreed": trials.get("disagreed", 0),
            "errors": trials.get("error", 0),
            "budget": trials.get("budget", 0),
            "agreement_rate": None if compared == 0 else agreed / compared,
            "cost_usd": float(from_pusd(entry["trial_cost_pusd"])),
        },
    }


def empty_version_stats() -> dict[str, Any]:
    """The figures of a version with no backtest and no trial."""
    return _finish(_empty())
