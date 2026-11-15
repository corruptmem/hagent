"""Invocation records: what a backtest needs to replay a past invocation.

Design section 10.7 says that history keeps the inputs and the context of
each invocation for the retention period. The rule stage of the router
produces these values, so this module records them around it, without a
change to ``rules/stage.py``:

- ``Recorder.begin`` and ``Recorder.finish`` run around each call of the
  rule stage (``BacktestComponent.try_rules``). ``finish`` writes one row
  with the input values and the full input states.
- ``RecordingContextCollector`` replaces ``RuleEngine.context``. It adds
  each context value that a rule uses to the record of its invocation.
- ``RecordingDecisions`` wraps the decision client of the rule engine. It
  adds each decision answer to the record, keyed by a hash of the request,
  so that a backtest can replay the answer of the same request.

Terms:

- **Item key**: the JSON form of a context item without ``name`` and
  ``cache_seconds``. Two rules that collect the same data share a key.
- **Request key**: the SHA-256 of the JSON form of a decision request: the
  model, the values and the questions.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from hagent_app.providers.decisions import (
    DecisionClient,
    DecisionRequest,
    DecisionResult,
)
from hagent_app.providers.meter import current_invocation
from hagent_app.rules.context import ContextCollector
from hagent_app.rules.spec import ContextItem
from hagent_app.rules.stage import answer_data
from hagent_app.storage import Database, transaction
from hagent_app.tools.base import StateView, ToolContext
from hagent_app.tools.executor import ToolExecutor

_LOG = logging.getLogger(__name__)

# A context value larger than this is not recorded. A backtest then reads
# it from the history of the turn, or skips the invocation.
MAX_RECORDED_VALUE = 256 * 1024
# The most pending records in memory, against a leak if ``finish`` never runs.
MAX_PENDING = 1000


def item_key(item: ContextItem) -> str:
    """The key of a context item in a record."""
    data = item.model_dump(mode="json", exclude={"cache_seconds", "name"})
    return json.dumps(data, sort_keys=True)


def request_key(request: DecisionRequest) -> str:
    """The key of a decision request in a record."""
    data = {
        "model": [request.model.provider, request.model.model],
        "values": request.values,
        "questions": [
            {"type": type(q).__name__, **asdict(q)} for q in request.questions
        ],
    }
    text = json.dumps(data, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


def decision_answers(
    request: DecisionRequest, result: DecisionResult
) -> dict[str, Any]:
    """The template form of the answers, as the rule stage passes them."""
    return {q.name: answer_data(result.answers.get(q.name)) for q in request.questions}


def state_data(view: StateView, entity_id: str) -> dict[str, Any] | None:
    """The JSON form of one state, or ``None`` if the entity is unknown."""
    state = view.get(entity_id)
    if state is None:
        return None
    return {
        "state": state.state,
        "attributes": _json_value(dict(state.attributes)),
        "last_changed": state.last_changed.isoformat(),
        "last_updated": state.last_updated.isoformat(),
    }


def _json_value(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


@dataclass(slots=True)
class _Pending:
    context: dict[str, Any] = field(default_factory=dict)
    decisions: dict[str, Any] = field(default_factory=dict)


class Recorder:
    """Collects the record of each invocation, and writes it at the end."""

    def __init__(self) -> None:
        self._db: Database | None = None
        self._pending: dict[str, _Pending] = {}

    def bind(self, db: Database | None) -> None:
        """Set or clear the database."""
        self._db = db
        if db is None:
            self._pending.clear()

    def begin(self, invocation_id: str) -> None:
        """Start the record of one invocation."""
        while len(self._pending) >= MAX_PENDING:
            self._pending.pop(next(iter(self._pending)))
        self._pending[invocation_id] = _Pending()

    def add_context(self, invocation_id: str | None, key: str, value: Any) -> None:
        """Add one context value. A value that is too large is left out."""
        pending = self._pending.get(invocation_id or "")
        if pending is None:
            return
        try:
            text = json.dumps(value, default=str)
        except ValueError:
            return
        if len(text) <= MAX_RECORDED_VALUE:
            pending.context[key] = json.loads(text)

    def add_decision(
        self, invocation_id: str | None, key: str, answers: Mapping[str, Any]
    ) -> None:
        """Add the answers of one decision request."""
        pending = self._pending.get(invocation_id or "")
        if pending is not None:
            pending.decisions[key] = _json_value(dict(answers))

    async def finish(  # noqa: PLR0913
        self,
        invocation_id: str,
        *,
        agent_id: str,
        source_key: str,
        inputs: Mapping[str, Any],
        states: StateView,
        at: datetime | None = None,
    ) -> None:
        """Write the record. A failure is logged: the invocation goes on."""
        pending = self._pending.pop(invocation_id, None) or _Pending()
        db = self._db
        if db is None:
            return
        created = (at or datetime.now(UTC)).isoformat()
        values = (
            invocation_id,
            agent_id,
            source_key,
            created,
            json.dumps(_json_value(dict(inputs)), ensure_ascii=False),
            json.dumps(
                {
                    eid: data
                    for eid in inputs
                    if (data := state_data(states, eid)) is not None
                },
                ensure_ascii=False,
            ),
            json.dumps(pending.context, ensure_ascii=False),
            json.dumps(pending.decisions, ensure_ascii=False),
        )
        try:
            await db.run(
                lambda c: c.execute(
                    "INSERT OR REPLACE INTO invocation_records (invocation_id,"
                    " agent_id, source_key, created_at, inputs, states, context,"
                    " decisions) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    values,
                )
            )
        except Exception:
            _LOG.exception("cannot write the record of invocation %s", invocation_id)


def purge(conn: sqlite3.Connection, keep_days: int, now: datetime) -> tuple[int, int]:
    """Delete invocation records and backtest cases older than ``keep_days``.

    Return the number of deleted records and cases. The backtest summaries
    stay: they hold no inputs.
    """
    cutoff = (now - timedelta(days=keep_days)).isoformat()
    with transaction(conn):
        records = conn.execute(
            "DELETE FROM invocation_records WHERE created_at < ?", (cutoff,)
        ).rowcount
        cases = conn.execute(
            "DELETE FROM rule_backtest_cases WHERE backtest_id IN"
            " (SELECT id FROM rule_backtests WHERE started_at < ?)",
            (cutoff,),
        ).rowcount
    return records, cases


class RecordingContextCollector(ContextCollector):
    """The context collector of the rule engine, with recording."""

    def __init__(
        self, db: Database, recorder: Recorder, *, max_entries: int = 1000
    ) -> None:
        super().__init__(db, max_entries=max_entries)
        self._recorder = recorder

    async def _one(
        self,
        ctx: ToolContext,
        executor: ToolExecutor,
        item: ContextItem,
        source_key: str,
    ) -> Any:
        value = await super()._one(ctx, executor, item, source_key)
        self._recorder.add_context(ctx.invocation_id, item_key(item), value)
        return value


class RecordingDecisions:
    """A decision client that records each answer for its invocation."""

    def __init__(self, inner: DecisionClient, recorder: Recorder) -> None:
        self._inner = inner
        self._recorder = recorder

    async def decide(
        self, request: DecisionRequest, *, agent_id: str | None
    ) -> DecisionResult:
        """Send the request, then record the answers."""
        result = await self._inner.decide(request, agent_id=agent_id)
        self._recorder.add_decision(
            current_invocation.get(),
            request_key(request),
            decision_answers(request, result),
        )
        return result
