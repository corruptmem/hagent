"""The backtest runner (design section 10.7).

``Backtester.run`` tests one rule version against the agent's history:

1. Select the past invocations of the agent in the window (7 days by
   default) that have an agent result: an agent turn that answered, or a
   trial turn next to a rule. Rules do not answer review, Assist and test
   invocations, so these sources do not count. The newest come first.
2. For each one, run the rule with the dry-run evaluator
   (``backtest/evaluate.py``). It writes nothing.
3. Compare the rule result with the agent result with the agreement
   template.
4. Store the counters, the agreement rate, the cost and each case.

``limit`` sets the most invocations that the rule matches and runs its
action for. A rule with a decision action has a default limit
(``backtest.decision_cases``), because each such invocation can cost one
decision request. Live decision requests count towards the budget of the
agent. When the budget is spent, the backtest stops with the status
``budget``. The cost of the backtest is the sum of its provider calls: the
runner sets the backtest ID as the current invocation of its own task.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError

from hagent_app.agents import Agent
from hagent_app.backtest import store
from hagent_app.backtest.config import BacktestConfig
from hagent_app.backtest.evaluate import DryRunEvaluator, PastInvocation
from hagent_app.backtest.store import Case, Totals
from hagent_app.providers.errors import BudgetExceededError
from hagent_app.providers.meter import current_invocation
from hagent_app.router import store as invocations
from hagent_app.router.models import NO_RULE_SOURCES, InvocationRequest
from hagent_app.rules.spec import DecisionAction, RuleSpec
from hagent_app.storage import Database

_LOG = logging.getLogger(__name__)


class BacktestError(Exception):
    """A backtest cannot start. ``code`` is stable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class BacktestOptions:
    """The options of one backtest.

    - ``version``: the rule version. ``None`` means the current version.
    - ``days``: the window. ``None`` means ``backtest.default_days``.
    - ``limit``: the most matched invocations. ``None`` means the default.
    - ``live``: false replays recorded decision answers only. An invocation
      with no recorded answer is then skipped.
    """

    version: int | None = None
    days: int | None = None
    limit: int | None = None
    live: bool = True


def _parse_time(text: str) -> datetime:
    value = datetime.fromisoformat(text)
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _candidates(
    conn: sqlite3.Connection, agent_id: str, since: str, until: str, limit: int
) -> list[PastInvocation]:
    sources = json.dumps(sorted(NO_RULE_SOURCES))
    rows = conn.execute(
        "SELECT i.id, i.created_at, i.started_at, i.source, i.source_key,"
        " i.request, i.result, i.path, i.status, i.turn_id,"
        " r.created_at AS recorded_at, r.inputs, r.states, r.context,"
        " r.decisions, tu.input AS turn_input,"
        " (SELECT t.turn_id FROM rule_trials t WHERE t.invocation_id = i.id"
        "  AND t.status IN ('agreed', 'disagreed') LIMIT 1) AS trial_turn,"
        " (SELECT t.agent_result FROM rule_trials t WHERE t.invocation_id = i.id"
        "  AND t.status IN ('agreed', 'disagreed') LIMIT 1) AS trial_result"
        " FROM invocations i"
        " LEFT JOIN invocation_records r ON r.invocation_id = i.id"
        " LEFT JOIN turns tu ON tu.id = i.turn_id"
        " WHERE i.agent_id = ? AND i.dry_run = 0"
        " AND i.created_at >= ? AND i.created_at <= ?"
        " AND i.source NOT IN (SELECT value FROM json_each(?))"
        " AND ((i.path = 'agent_turn' AND i.status = 'ok')"
        "  OR EXISTS (SELECT 1 FROM rule_trials t WHERE t.invocation_id = i.id"
        "  AND t.status IN ('agreed', 'disagreed')))"
        " ORDER BY i.created_at DESC, i.rowid DESC LIMIT ?",
        (agent_id, since, until, sources, limit),
    ).fetchall()
    found = []
    for row in rows:
        request = InvocationRequest.from_json(row["request"])
        turn_input = json.loads(row["turn_input"]) if row["turn_input"] else {}
        if row["inputs"] is not None:
            inputs = json.loads(row["inputs"])
        else:
            inputs = turn_input.get("inputs") or {}
        if row["path"] == "agent_turn" and row["status"] == "ok":
            result = None if row["result"] is None else json.loads(row["result"])
            turn_id = row["turn_id"]
        else:
            raw = row["trial_result"]
            result = None if raw is None else json.loads(raw)
            turn_id = row["trial_turn"]
        at = row["recorded_at"] or row["started_at"] or row["created_at"]
        found.append(
            PastInvocation(
                invocation_id=row["id"],
                at=_parse_time(at),
                source=request.source,
                source_key=request.source_key,
                data=request.turn_data,
                inputs=inputs,
                agent_result=result,
                turn_id=turn_id,
                states=json.loads(row["states"]) if row["states"] else {},
                context=json.loads(row["context"]) if row["context"] else {},
                decisions=json.loads(row["decisions"]) if row["decisions"] else {},
            )
        )
    return found


def _current_version(
    conn: sqlite3.Connection, agent_id: str, rule_id: str
) -> int | None:
    row = conn.execute(
        "SELECT current_version FROM rules WHERE id = ? AND agent_id = ?"
        " AND deleted_at IS NULL",
        (rule_id, agent_id),
    ).fetchone()
    return None if row is None else int(row[0])


def _spec(conn: sqlite3.Connection, agent_id: str, rule_id: str, version: int) -> Any:
    row = conn.execute(
        "SELECT v.spec FROM rule_versions v JOIN rules r ON r.id = v.rule_id"
        " WHERE r.agent_id = ? AND r.deleted_at IS NULL AND v.rule_id = ?"
        " AND v.version = ?",
        (agent_id, rule_id, version),
    ).fetchone()
    return None if row is None else json.loads(row[0])


class Backtester:
    """Runs backtests. One backtest at a time for each rule."""

    def __init__(
        self,
        db: Database,
        evaluator: DryRunEvaluator,
        config: BacktestConfig | None = None,
    ) -> None:
        self._db = db
        self._evaluator = evaluator
        self.config = config or BacktestConfig()
        self._running: set[str] = set()

    async def run(
        self, agent: Agent, rule_id: str, options: BacktestOptions | None = None
    ) -> dict[str, Any]:
        """Run one backtest and return it with its cases.

        Raise ``BacktestError`` with ``rule_not_found``,
        ``version_not_found``, ``invalid_rule`` or ``backtest_running``.
        """
        options = options or BacktestOptions()
        version = options.version or await self._db.run(
            lambda c: _current_version(c, agent.id, rule_id)
        )
        if version is None:
            msg = "the agent has no rule with this ID"
            raise BacktestError("rule_not_found", msg)
        raw = await self._db.run(lambda c: _spec(c, agent.id, rule_id, version))
        if raw is None:
            msg = f"the rule has no version {version}"
            raise BacktestError("version_not_found", msg)
        try:
            spec = RuleSpec.model_validate(raw)
        except ValidationError as err:
            msg = f"the rule version is not valid: {err.error_count()} errors"
            raise BacktestError("invalid_rule", msg) from err
        config = self.config
        days = min(max(options.days or config.default_days, 1), config.max_days)
        default = (
            config.decision_cases
            if isinstance(spec.action, DecisionAction)
            else config.max_cases
        )
        limit = min(max(options.limit or default, 1), config.max_cases)
        if rule_id in self._running:
            msg = "a backtest of this rule runs already"
            raise BacktestError("backtest_running", msg)
        self._running.add(rule_id)
        try:
            # Its own task: the backtest ID is the current invocation there,
            # so its provider calls carry it, and the caller's value stays.
            return await asyncio.create_task(
                self._run(
                    agent,
                    rule_id=rule_id,
                    version=version,
                    spec=spec,
                    days=days,
                    limit=limit,
                    live=options.live,
                )
            )
        finally:
            self._running.discard(rule_id)

    async def _run(  # noqa: PLR0913
        self,
        agent: Agent,
        *,
        rule_id: str,
        version: int,
        spec: RuleSpec,
        days: int,
        limit: int,
        live: bool,
    ) -> dict[str, Any]:
        backtest_id = uuid.uuid4().hex
        current_invocation.set(backtest_id)
        until = datetime.now(UTC)
        since = until - timedelta(days=days)
        await self._db.run(
            lambda c: store.insert_backtest(
                c,
                backtest_id,
                agent_id=agent.id,
                rule_id=rule_id,
                version=version,
                agent_version=agent.version,
                since=since.isoformat(),
                until=until.isoformat(),
                max_cases=limit,
            )
        )
        totals = Totals()
        cases: list[Case] = []
        status, error = "ok", None
        try:
            past = await self._db.run(
                lambda c: _candidates(
                    c,
                    agent.id,
                    since.isoformat(),
                    until.isoformat(),
                    self.config.max_cases,
                )
            )
            for item in past:
                case = await self._evaluator.evaluate(
                    agent, spec, item, run_id=backtest_id, live_decisions=live
                )
                cases.append(case)
                totals.add(case)
                if totals.matched >= limit:
                    break
        except asyncio.CancelledError:
            status, error = "cancelled", "the backtest was cancelled"
            await asyncio.shield(
                self._finish(backtest_id, status, totals, cases, error)
            )
            raise
        except BudgetExceededError as err:
            status, error = "budget", str(err)
        except Exception as err:
            _LOG.exception("backtest %s failed", backtest_id)
            status, error = "failed", f"{type(err).__name__}: {err}"
        return await self._finish(backtest_id, status, totals, cases, error)

    async def _finish(
        self,
        backtest_id: str,
        status: str,
        totals: Totals,
        cases: list[Case],
        error: str | None,
    ) -> dict[str, Any]:
        cost = await self._db.run(lambda c: invocations.cost_pusd(c, backtest_id))

        def run(conn: sqlite3.Connection) -> dict[str, Any] | None:
            store.finish_backtest(
                conn,
                backtest_id,
                status=status,
                totals=totals,
                cases=cases,
                cost_pusd=cost,
                error=error,
            )
            row = conn.execute(
                "SELECT agent_id FROM rule_backtests WHERE id = ?", (backtest_id,)
            ).fetchone()
            return store.get_backtest(conn, row[0], backtest_id)

        found = await self._db.run(run)
        assert found is not None  # noqa: S101 - written above
        return found
