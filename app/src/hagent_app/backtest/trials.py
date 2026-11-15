"""Trials: dry-run agent turns next to rules that answer (design 10.1).

When a rule with ``trial.enabled`` answers an invocation, the runtime also
runs an agent turn on a sample of these invocations (``trial.rate``, 1 in 5
by default). It compares the two results with the agreement template. Only
the rule result takes effect:

- The trial turn is a dry run (``TurnInput.dry_run``). Its tools check the
  scope and record changes, but do not make them. It does not go into the
  running summaries.
- The trial starts after the rule answered, in its own task. The caller of
  the invocation never waits for it.
- The trial counts towards the budget of the agent. Before the turn, the
  runner reads the budget. If the agent budget or the installation budget
  is spent, the trial ends with the status ``budget`` and no turn runs.
  During the turn, the meter stops it when the budget is spent.
- One trial at a time runs for each rule. A match during a trial gets no
  trial.
- The trial ID is the current invocation of the trial task, so that the
  provider calls of the trial carry it and its cost is exact.
"""

from __future__ import annotations

import asyncio
import logging
import random
import uuid
from collections.abc import Callable
from typing import Any, Protocol

from hagent_app.agents import Agent
from hagent_app.backtest import store
from hagent_app.backtest.evaluate import agreement
from hagent_app.providers.meter import BudgetState, current_invocation
from hagent_app.router import store as invocations
from hagent_app.router.rules import RuleOutcome, RuleRequest
from hagent_app.rules.spec import RuleSpec
from hagent_app.rules.stage import visible_entities
from hagent_app.rules.templates import (
    ScopedStates,
    TemplateClock,
    TemplateEngine,
    TemplateError,
)
from hagent_app.storage import Database
from hagent_app.tools.base import StateView
from hagent_app.tools.executor import ToolExecutor
from hagent_app.turns.models import TurnInput, TurnResult

_LOG = logging.getLogger(__name__)


class BudgetReader(Protocol):
    """What a trial needs from the meter."""

    async def budget_state(self, agent_id: str | None) -> BudgetState: ...


class TurnRunner(Protocol):
    """What a trial needs from ``TurnsComponent``."""

    async def run_turn(
        self, agent: Agent, turn: TurnInput, *, states: StateView | None = None
    ) -> TurnResult: ...


class TrialRunner:
    """Starts and tracks trials."""

    def __init__(  # noqa: PLR0913
        self,
        db: Database,
        turns: Callable[[], TurnRunner | None],
        meter: Callable[[], BudgetReader | None],
        executor: Callable[[], ToolExecutor],
        *,
        clock: Callable[[], TemplateClock] = TemplateClock,
        render_seconds: float = 1.0,
        sample: Callable[[], float] = random.random,
    ) -> None:
        self._db = db
        self._turns = turns
        self._meter = meter
        self._executor = executor
        self._clock = clock
        self._render_seconds = render_seconds
        self._sample = sample
        self._running: dict[str, asyncio.Task[None]] = {}

    @property
    def tasks(self) -> list[asyncio.Task[None]]:
        """The trials that run now."""
        return list(self._running.values())

    def maybe_start(
        self, request: RuleRequest, outcome: RuleOutcome, spec: RuleSpec
    ) -> bool:
        """Start a trial if the rule asks for one and the sample hits."""
        trial = spec.trial
        rule_id = outcome.rule_id
        if (
            not outcome.answered
            or rule_id is None
            or outcome.rule_version is None
            or trial is None
            or not trial.enabled
            or rule_id in self._running
            or self._sample() >= trial.rate
        ):
            return False
        task = asyncio.create_task(
            self._run(request, rule_id, outcome.rule_version, outcome.result, spec),
            name=f"trial-{rule_id}",
        )
        self._running[rule_id] = task
        task.add_done_callback(lambda _t: self._running.pop(rule_id, None))
        return True

    async def stop(self) -> None:
        """Cancel every running trial and wait for it to record the end."""
        tasks = list(self._running.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(
        self,
        request: RuleRequest,
        rule_id: str,
        version: int,
        rule_result: Any,
        spec: RuleSpec,
    ) -> None:
        trial_id = uuid.uuid4().hex
        # This task has its own copy of the context: the live invocation
        # keeps its ID, and the trial's provider calls carry the trial ID.
        current_invocation.set(trial_id)
        agent = request.agent
        await self._db.run(
            lambda c: store.insert_trial(
                c,
                trial_id,
                agent_id=agent.id,
                rule_id=rule_id,
                version=version,
                invocation_id=request.invocation_id,
                rule_result=rule_result,
            )
        )
        status, turn_id, agent_result, detail = "error", None, None, None
        try:
            status, turn_id, agent_result, detail = await self._trial(
                request, trial_id, rule_result, spec
            )
        except asyncio.CancelledError:
            await asyncio.shield(
                self._finish(trial_id, "cancelled", None, None, "cancelled")
            )
            raise
        except Exception as err:
            _LOG.exception("trial %s failed", trial_id)
            detail = f"{type(err).__name__}: {err}"
        await self._finish(trial_id, status, turn_id, agent_result, detail)

    async def _trial(
        self, request: RuleRequest, trial_id: str, rule_result: Any, spec: RuleSpec
    ) -> tuple[str, str | None, Any, str | None]:
        agent = request.agent
        spent = await self._spent(agent.id)
        if spent is not None:
            return "budget", None, None, spent
        turns = self._turns()
        if turns is None:
            return "error", None, None, "agent turns are not available"
        invocation = request.request
        turn = await turns.run_turn(
            agent,
            TurnInput(
                source=invocation.source,
                inputs=dict(request.inputs),
                data=invocation.turn_data,
                invocation_id=trial_id,
                conversation_id=invocation.conversation_id,
                result_schema=invocation.result_schema,
                dry_run=True,
            ),
        )
        if not turn.ok:
            status = "budget" if turn.stop_reason == "budget" else "error"
            return status, turn.turn_id, None, turn.error or turn.stop_reason
        env = self._executor().env
        view = ScopedStates.build(
            env.states, visible_entities(agent, env.registry.last())
        )
        engine = TemplateEngine(view, self._clock(), self._render_seconds)
        variables = {
            "source": invocation.source,
            "inputs": dict(request.inputs),
            "data": invocation.turn_data,
            "context": {},
        }
        try:
            agreed = await agreement(engine, spec, variables, rule_result, turn.result)
        except TemplateError as err:
            return "error", turn.turn_id, turn.result, f"agreement: {err}"
        return (
            "agreed" if agreed else "disagreed",
            turn.turn_id,
            turn.result,
            None,
        )

    async def _spent(self, agent_id: str) -> str | None:
        """Why the budget stops the trial, or ``None``."""
        meter = self._meter()
        if meter is None:
            return None
        if (await meter.budget_state(agent_id)).remaining_usd <= 0:
            return "the daily budget of the agent is spent"
        if (await meter.budget_state(None)).remaining_usd <= 0:
            return "the daily budget of the installation is spent"
        return None

    async def _finish(
        self,
        trial_id: str,
        status: str,
        turn_id: str | None,
        agent_result: Any,
        detail: str | None,
    ) -> None:
        def run(conn: Any) -> None:
            store.finish_trial(
                conn,
                trial_id,
                status=status,
                turn_id=turn_id,
                agent_result=agent_result,
                detail=detail,
                cost_pusd=invocations.cost_pusd(conn, trial_id),
            )

        try:
            await self._db.run(run)
        except Exception:
            _LOG.exception("cannot record the end of trial %s", trial_id)
