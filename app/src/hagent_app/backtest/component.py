"""The backtest component: records, backtests, trials and the rule routes.

``BacktestComponent`` is the rule stage of the router. It wraps
``RulesComponent``:

1. ``Recorder.begin`` starts the record of the invocation.
2. ``RulesComponent.try_rules`` runs the rules. The recording context
   collector and the recording decision client add their values to the
   record.
3. ``Recorder.finish`` writes the record, also when no rule is active, so
   that a later draft rule can run a backtest on this invocation.
4. If a rule answered and its spec turns on trials, ``TrialRunner`` can
   start a trial.

``default_components`` lists it after ``rules`` and ``turns`` and before
``router``. It sets ``router.rules`` to this component, gives
``RulesComponent`` the recording decision source, and plugs the backtest
into the ``self.backtest_rule`` tool and into the rule statistics.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import sqlite3
from collections import OrderedDict
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from aiohttp import web
from pydantic import ValidationError

from hagent_app.agents import Agent, AgentReader
from hagent_app.backtest import records
from hagent_app.backtest import store as backtest_store
from hagent_app.backtest.config import BacktestConfig
from hagent_app.backtest.evaluate import DryRunEvaluator
from hagent_app.backtest.records import (
    Recorder,
    RecordingContextCollector,
    RecordingDecisions,
)
from hagent_app.backtest.runner import Backtester, BacktestOptions
from hagent_app.backtest.trials import BudgetReader, TrialRunner, TurnRunner
from hagent_app.providers.decisions import DecisionClient
from hagent_app.router.rules import RuleOutcome, RuleRequest
from hagent_app.rules.config import RulesConfig
from hagent_app.rules.spec import RuleSpec

if TYPE_CHECKING:
    from hagent_app.app import AppContext
    from hagent_app.http.events import ChangeEvents
    from hagent_app.rules import RulesComponent
    from hagent_app.storage import Database
    from hagent_app.tools import ToolsComponent

_LOG = logging.getLogger(__name__)

_SPEC_CACHE = 256


class BacktestUnavailableError(RuntimeError):
    """The backtest component is not running."""


class Providers(Protocol):
    """What the component needs from ``ProvidersComponent``."""

    @property
    def decisions(self) -> DecisionClient:
        """The decision client."""
        ...

    @property
    def meter(self) -> BudgetReader:
        """The meter, for the budget check of a trial."""
        ...


class RecordingDecisionSource:
    """The decision source of ``RulesComponent``: each answer is recorded."""

    def __init__(self, providers: Providers, recorder: Recorder) -> None:
        self._providers = providers
        self._recorder = recorder

    @property
    def decisions(self) -> DecisionClient:
        """The decision client of the providers, with recording."""
        return RecordingDecisions(self._providers.decisions, self._recorder)


class BacktestComponent:
    """Owns invocation records, backtests and trials (design 10.1, 10.7)."""

    name = "backtest"

    def __init__(
        self,
        tools: ToolsComponent,
        providers: Providers,
        events: ChangeEvents | None = None,
    ) -> None:
        self._tools = tools
        self._providers = providers
        self._events = events
        self.recorder = Recorder()
        # ``default_components`` sets these two.
        self.rules: RulesComponent | None = None
        self.turns: TurnRunner | None = None
        self.config = BacktestConfig()
        self._backtester: Backtester | None = None
        self._trials: TrialRunner | None = None
        self._reader: AgentReader | None = None
        self._db: Database | None = None
        self._purge_task: asyncio.Task[None] | None = None
        self._specs: OrderedDict[tuple[str, int], RuleSpec | None] = OrderedDict()
        # The trial sample: a number from 0 to 1 for each match.
        self.sample: Callable[[], float] = random.random

    def decision_source(self) -> RecordingDecisionSource:
        """The decision source to give ``RulesComponent``."""
        return RecordingDecisionSource(self._providers, self.recorder)

    # ------------------------------------------------------------ lifecycle

    async def start(self, ctx: AppContext) -> None:
        """Plug into the rule engine, then start the purge loop."""
        rules = self._rules()
        self.config = ctx.config.section("backtest", BacktestConfig)
        rules_config = ctx.config.section("rules", RulesConfig)
        self._db = ctx.db
        self._reader = AgentReader(ctx.db)
        self.recorder.bind(ctx.db)
        rules.engine.context = RecordingContextCollector(
            ctx.db, self.recorder, max_entries=rules_config.cache_entries
        )
        evaluator = DryRunEvaluator(
            ctx.db,
            self._tools.executor,
            lambda: self._providers.decisions,
            clock=rules.clock,
            render_seconds=rules_config.render_seconds,
        )
        self._backtester = Backtester(ctx.db, evaluator, self.config)
        self._trials = TrialRunner(
            ctx.db,
            lambda: self.turns,
            self._meter,
            self._tools.executor,
            clock=rules.clock,
            render_seconds=rules_config.render_seconds,
            sample=self._sample,
        )
        rules.store.version_extras = backtest_store.version_stats
        rules.agent_state.backtests = self
        await ctx.db.run(backtest_store.close_running)
        await self.purge()
        self._purge_task = asyncio.create_task(self._purge_loop(), name="purge")

    async def stop(self) -> None:
        """Stop the trials and the purge loop."""
        if self._purge_task is not None:
            self._purge_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._purge_task
            self._purge_task = None
        if self._trials is not None:
            await self._trials.stop()
        if self.rules is not None:
            self.rules.agent_state.backtests = None
            self.rules.store.version_extras = None
        self._backtester = None
        self._trials = None
        self.recorder.bind(None)
        self._db = None

    def _rules(self) -> RulesComponent:
        if self.rules is None:
            msg = "the backtest component has no rules component"
            raise BacktestUnavailableError(msg)
        return self.rules

    def _sample(self) -> float:
        # Read on each match, so that a test can replace ``sample``.
        return self.sample()

    def _meter(self) -> BudgetReader | None:
        try:
            return self._providers.meter
        except Exception:
            return None

    def routes(self) -> list[web.RouteDef]:
        """The rule routes of the management API (``docs/api.md``)."""
        from hagent_app.backtest.api import RulesApi  # noqa: PLC0415 - import cycle

        return RulesApi(self._running).routes()

    def _running(self) -> BacktestComponent:
        if self._backtester is None:
            from hagent_app.http.errors import ApiError  # noqa: PLC0415

            raise ApiError(503, "not_ready", "the rule engine is not running")
        return self

    # ------------------------------------------------------------ rule stage

    async def try_rules(self, request: RuleRequest) -> RuleOutcome:
        """The ``RuleStage`` of the router, with records and trials."""
        rules = self._rules()
        self.recorder.begin(request.invocation_id)
        try:
            outcome = await rules.try_rules(request)
        finally:
            await self.recorder.finish(
                request.invocation_id,
                agent_id=request.agent.id,
                source_key=request.request.source_key,
                inputs=request.inputs,
                states=self._tools.executor().env.states,
            )
        if outcome.answered and self._trials is not None:
            try:
                spec = await self._spec(request.agent, outcome)
                if spec is not None:
                    self._trials.maybe_start(request, outcome, spec)
            except Exception:
                _LOG.exception("cannot start a trial")
        return outcome

    async def _spec(self, agent: Agent, outcome: RuleOutcome) -> RuleSpec | None:
        """The spec of the rule version that answered. Versions never change."""
        if outcome.rule_id is None or outcome.rule_version is None:
            return None
        key = (outcome.rule_id, outcome.rule_version)
        if key in self._specs:
            self._specs.move_to_end(key)
            return self._specs[key]
        raw = await self._rules().store.get_spec(agent.id, *key)
        spec = None
        if raw is not None:
            with contextlib.suppress(ValidationError):
                spec = RuleSpec.model_validate(raw)
        self._specs[key] = spec
        while len(self._specs) > _SPEC_CACHE:
            self._specs.popitem(last=False)
        return spec

    # ------------------------------------------------------------ backtests

    @property
    def backtester(self) -> Backtester:
        """The running backtester. Raise before start."""
        if self._backtester is None:
            msg = "the backtest component is not running"
            raise BacktestUnavailableError(msg)
        return self._backtester

    @property
    def trials(self) -> TrialRunner:
        """The running trial runner. Raise before start."""
        if self._trials is None:
            msg = "the backtest component is not running"
            raise BacktestUnavailableError(msg)
        return self._trials

    async def backtest_rule(
        self, agent: Agent, rule_id: str, options: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Run one backtest for ``agent`` (the ``self.backtest_rule`` tool).

        ``agent`` is the snapshot of the invocation. The caller checked that
        the agent owns the rule.
        """
        result = await self.backtester.run(
            agent,
            rule_id,
            BacktestOptions(
                version=options.get("version"),
                days=options.get("days"),
                limit=options.get("limit"),
                live=bool(options.get("live", True)),
            ),
        )
        if self._events is not None:
            self._events.publish(
                "rules",
                "backtest_finished",
                rule_id,
                {"agent_id": agent.id, "backtest_id": result["id"]},
            )
        return result

    async def agent(self, agent_id: str) -> Agent | None:
        """The current snapshot of a live agent, or ``None``."""
        if self._reader is None:
            msg = "the backtest component is not running"
            raise BacktestUnavailableError(msg)
        return await self._reader.get(agent_id)

    def publish(self, action: str, rule_id: str, data: Mapping[str, Any]) -> None:
        """Send a ``rules`` change event."""
        if self._events is not None:
            self._events.publish("rules", action, rule_id, data)

    async def db_run(self, fn: Any) -> Any:
        """Run ``fn(conn)`` on the database."""
        if self._db is None:
            msg = "the backtest component is not running"
            raise BacktestUnavailableError(msg)
        return await self._db.run(fn)

    # ------------------------------------------------------------ retention

    async def purge(self) -> tuple[int, int]:
        """Delete records and cases older than the retention period."""
        db = self._db
        if db is None:
            return 0, 0
        keep = self.config.keep_days

        def run(conn: sqlite3.Connection) -> tuple[int, int]:
            return records.purge(conn, keep, datetime.now(UTC))

        try:
            found = await db.run(run)
        except Exception:
            _LOG.exception("cannot purge old invocation records")
            return 0, 0
        if any(found):
            _LOG.info("purged %d invocation records and %d backtest cases", *found)
        return found

    async def _purge_loop(self) -> None:
        while True:
            await asyncio.sleep(max(self.config.purge_hours, 0.01) * 3600)
            await self.purge()
