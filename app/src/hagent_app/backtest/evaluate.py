"""The dry-run evaluator: one rule version against one past invocation.

A backtest (design section 10.7) runs the rule against recorded history. It
must not write, and it must use the values of the past invocation, not the
values of today. ``DryRunEvaluator.evaluate`` runs the same steps as the
live rule stage (``rules/stage.py``), with these differences:

- **Clock**: ``now()`` returns the time of the past invocation.
- **States**: the input entities have their recorded states. The other
  entities in the read scope have their current states.
- **Context**: each item comes from history, never from a live call that
  returns data for the wrong time. See ``HistoryContext``.
- **Decisions**: the evaluator replays the recorded answers of the same
  request. Without a recorded answer, it sends the request only if the
  caller permits live requests. The case says ``replayed`` or ``live``.
- **Writes**: the evaluator runs the write checks, renders the data,
  checks the rendered call as the executor does, and records each write in
  the case. It never calls the tool executor for a
  write, so a write never reaches HA.
- **Statistics**: the evaluator does not touch the live rule statistics or
  the live result cache.

Then it compares the rule result with the result of the agent turn, with
the agreement template of the rule. The template receives ``rule_result``
and ``agent_result``. A rule without an agreement template agrees when the
two results have the same JSON form.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
from typing import Any

from pydantic import ValidationError

from hagent_app.agents import Agent
from hagent_app.backtest.records import item_key, request_key
from hagent_app.backtest.store import Case
from hagent_app.ha import State
from hagent_app.providers.decisions import (
    DecisionClient,
    DecisionModel,
    DecisionRequest,
)
from hagent_app.providers.errors import BudgetExceededError
from hagent_app.router.overlay import StateOverlay, sample_state
from hagent_app.rules.checks import _check_store, check_action_response, check_writes
from hagent_app.rules.context import RuleFailure
from hagent_app.rules.spec import (
    ActionResponseItem,
    ContextItem,
    DecisionAction,
    HistoryItem,
    InvocationHistoryItem,
    RuleSpec,
    StoreFileItem,
    TemplateAction,
)
from hagent_app.rules.stage import (
    _question,
    answer_data,
    decision_values,
    visible_entities,
)
from hagent_app.rules.templates import (
    DECLINE,
    ScopedStates,
    TemplateClock,
    TemplateEngine,
    TemplateError,
    is_true,
    parse_result,
)
from hagent_app.storage import Database
from hagent_app.tools.base import ToolContext
from hagent_app.tools.executor import ToolExecutor, needs_approval
from hagent_app.tools.ha_tools import CallService, CallServiceArgs, scope_of
from hagent_app.tools.scope import ScopeDeniedError, check_read


def fixed_clock(tz: tzinfo | None, at: datetime) -> TemplateClock:
    """A template clock that stays at one time: the past invocation.

    The fixed time is a field of ``TemplateClock``, so that the render pool
    sends it to the worker process.
    """
    return TemplateClock(tz, at)


@dataclass(frozen=True, slots=True)
class PastInvocation:
    """One past invocation with what a backtest needs to replay it.

    - ``data``: the caller data, with ``text``.
    - ``states``: the recorded input states. Empty for an invocation from
      before migration 0750: the input values then come from the turn.
    - ``context``: recorded context values, by item key.
    - ``decisions``: recorded decision answers, by request key.
    - ``turn_id``: the agent turn (or trial turn) that gave
      ``agent_result``.
    """

    invocation_id: str
    at: datetime
    source: str
    source_key: str
    data: Mapping[str, Any]
    inputs: Mapping[str, Any]
    agent_result: Any
    turn_id: str | None = None
    states: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    context: Mapping[str, Any] = field(default_factory=dict)
    decisions: Mapping[str, Any] = field(default_factory=dict)


class SkipCase(Exception):  # noqa: N818 - a case outcome
    """The history does not hold what the case needs."""


class _Declined(Exception):  # noqa: N818 - a case outcome
    """The rule declined."""


def past_states(past: PastInvocation) -> dict[str, State]:
    """The recorded input states, as ``State`` objects."""
    found: dict[str, State] = {}
    for entity_id, value in past.inputs.items():
        recorded = past.states.get(entity_id)
        if recorded is not None:
            found[entity_id] = State.from_ha({"entity_id": entity_id, **recorded})
        elif value is not None:
            found[entity_id] = sample_state(entity_id, {"state": str(value)})
    return found


class HistoryContext:
    """Context items of a past invocation (design section 10.5).

    Every item gets the scope check of the live rule stage first, also a
    recorded value. Then:

    - A value that a rule recorded for the invocation comes first.
    - ``history``: ``ha.get_statistics`` through the tool executor, with the
      window that ends at the time of the invocation. It only reads.
    - ``invocation_history``: the invocations from the same source before
      the past invocation.
    - ``action_response``: the response of the same data action in the
      agent turn of the invocation (``turn_tool_calls``). The evaluator
      never calls the action again: it would return data for the wrong
      time. Without a match, the case is skipped.
    - ``store_file``: the same read in the agent turn, else the current
      content of the file through ``knowledge.read``.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    async def collect(
        self,
        ctx: ToolContext,
        executor: ToolExecutor,
        items: list[ContextItem],
        past: PastInvocation,
    ) -> dict[str, Any]:
        """Collect each item. Raise ``SkipCase``, ``RuleFailure`` or a denial."""
        values: dict[str, Any] = {}
        for item in items:
            values[item.name] = await self._one(ctx, executor, item, past)
        return values

    async def _one(
        self,
        ctx: ToolContext,
        executor: ToolExecutor,
        item: ContextItem,
        past: PastInvocation,
    ) -> Any:
        env = executor.env
        if isinstance(item, HistoryItem):
            if not ctx.definition.tools.enabled("ha"):
                raise ScopeDeniedError("tool_off", "the ha tools are off")
            check_read(scope_of(ctx, env), item.entity_ids)
        elif isinstance(item, ActionResponseItem):
            check_action_response(ctx, env, item)
        elif isinstance(item, StoreFileItem):
            _check_store(ctx, item)
        key = item_key(item)
        if key in past.context:
            return past.context[key]
        if isinstance(item, HistoryItem):
            return await _call(
                executor,
                ctx,
                item.name,
                "ha.get_statistics",
                {
                    "statistic_ids": item.entity_ids,
                    "start": (past.at - timedelta(hours=item.hours)).isoformat(),
                    "end": past.at.isoformat(),
                    "period": item.period,
                },
            )
        if isinstance(item, InvocationHistoryItem):
            return await self._invocations(ctx.agent.id, item, past)
        if isinstance(item, ActionResponseItem):
            found, data = await self._turn_call(past.turn_id, _response_matcher(item))
            if not found:
                msg = f"context item {item.name}: no recorded response"
                raise SkipCase(msg)
            return data.get("response") if isinstance(data, Mapping) else None
        found, data = await self._turn_call(past.turn_id, _read_matcher(item))
        if found:
            return data
        arguments: dict[str, Any] = {"store_id": item.store_id, "path": item.path}
        if item.section is not None:
            arguments["section"] = item.section
        return await _call(executor, ctx, item.name, "knowledge.read", arguments)

    async def _turn_call(
        self, turn_id: str | None, matches: Callable[[str, Mapping[str, Any]], bool]
    ) -> tuple[bool, Any]:
        """The data of a successful tool call of the turn that ``matches``."""
        if turn_id is None:
            return False, None

        def run(conn: sqlite3.Connection) -> tuple[bool, Any]:
            rows = conn.execute(
                "SELECT tool, arguments, result FROM turn_tool_calls"
                " WHERE turn_id = ? AND ok = 1 ORDER BY seq",
                (turn_id,),
            ).fetchall()
            for tool, arguments, result in rows:
                args = json.loads(arguments)
                if not isinstance(args, Mapping) or not matches(tool, args):
                    continue
                data = json.loads(result)
                if not isinstance(data, Mapping) or data.get("ok") is not True:
                    continue
                inner = data.get("data")
                if isinstance(inner, Mapping) and inner.get("dry_run"):
                    continue
                return True, inner
            return False, None

        return await self._db.run(run)

    async def _invocations(
        self, agent_id: str, item: InvocationHistoryItem, past: PastInvocation
    ) -> list[dict[str, Any]]:
        def run(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute(
                "SELECT created_at, finished_at, status, path, rule_id,"
                " rule_version, request, result FROM invocations"
                " WHERE agent_id = ? AND source_key = ? AND dry_run = 0"
                " AND id != ? AND created_at < ?"
                " AND status IN ('ok', 'fallback', 'error')"
                " ORDER BY created_at DESC LIMIT ?",
                (
                    agent_id,
                    past.source_key,
                    past.invocation_id,
                    past.at.isoformat(),
                    item.limit,
                ),
            ).fetchall()
            return [
                {
                    "created_at": row["created_at"],
                    "finished_at": row["finished_at"],
                    "status": row["status"],
                    "path": row["path"],
                    "rule_id": row["rule_id"],
                    "rule_version": row["rule_version"],
                    "data": json.loads(row["request"]).get("data") or {},
                    "result": (
                        None if row["result"] is None else json.loads(row["result"])
                    ),
                }
                for row in rows
            ]

        return await self._db.run(run)


def _same(a: Any, b: Any) -> bool:
    return json.dumps(a or None, sort_keys=True) == json.dumps(
        b or None, sort_keys=True
    )


def _response_matcher(
    item: ActionResponseItem,
) -> Callable[[str, Mapping[str, Any]], bool]:
    domain, service = item.service.split(".", 1)

    def matches(tool: str, args: Mapping[str, Any]) -> bool:
        return (
            tool == "ha.call_service"
            and args.get("domain") == domain
            and args.get("service") == service
            and args.get("return_response") is True
            and _same(args.get("target"), item.target)
            and _same(args.get("data"), item.data)
        )

    return matches


def _read_matcher(item: StoreFileItem) -> Callable[[str, Mapping[str, Any]], bool]:
    def matches(tool: str, args: Mapping[str, Any]) -> bool:
        return (
            tool == "knowledge.read"
            and args.get("store_id") == item.store_id
            and args.get("path") == item.path
            and args.get("section") == item.section
        )

    return matches


async def _call(
    executor: ToolExecutor,
    ctx: ToolContext,
    name: str,
    tool: str,
    arguments: Mapping[str, Any],
) -> Any:
    result = await executor.execute(ctx, tool, arguments)
    if not result.ok:
        msg = f"context item {name}: {result.error_code}: {result.error}"
        raise RuleFailure(msg)
    return result.data


def _json_value(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


_CALL_SERVICE = CallService()


async def _check_write(
    ctx: ToolContext,
    executor: ToolExecutor,
    service: str,
    entity_id: str,
    data: Mapping[str, Any],
) -> None:
    """The scope and approval check of one rendered write.

    It runs the ``authorize`` step of ``ha.call_service``, as the executor
    does for a live write. It never runs the call and writes no audit row.
    """
    domain, name = service.split(".", 1)
    try:
        args = CallServiceArgs.model_validate(
            {
                "domain": domain,
                "service": name,
                "target": {"entity_id": [entity_id]},
                "data": dict(data),
            }
        )
    except ValidationError as err:
        msg = f"write to {entity_id}: invalid_arguments: {err.error_count()} errors"
        raise RuleFailure(msg) from err
    prepared = await _CALL_SERVICE.authorize(ctx, executor.env, args)
    if needs_approval(ctx.definition, _CALL_SERVICE, prepared):
        msg = f"the write to {entity_id} needs approval"
        raise ScopeDeniedError("approval_required", msg)


class DryRunEvaluator:
    """Runs one rule version against past invocations. It writes nothing.

    ``executor`` is a factory of the tool executor of the app. The
    evaluator uses a copy with no approval gate, and a tool context with
    ``dry_run`` set, so that no tool call can change HA or a store.
    """

    def __init__(
        self,
        db: Database,
        executor: Callable[[], ToolExecutor],
        decisions: Callable[[], DecisionClient],
        *,
        clock: Callable[[], TemplateClock] = TemplateClock,
        render_seconds: float = 1.0,
    ) -> None:
        self._executor = executor
        self._decisions = decisions
        self._clock = clock
        self._render_seconds = render_seconds
        self.context = HistoryContext(db)

    async def evaluate(  # noqa: PLR0911
        self,
        agent: Agent,
        spec: RuleSpec,
        past: PastInvocation,
        *,
        run_id: str,
        live_decisions: bool,
    ) -> Case:
        """Run the rule for ``past`` and compare it with the agent result.

        Raise ``BudgetExceededError`` when a live decision request finds the
        budget spent. The caller stops the backtest.
        """
        case = Case(past.invocation_id, past.at.isoformat(), "error")
        case.agent_result = past.agent_result
        executor = self._executor().without_approval()
        env = executor.env
        overlay = StateOverlay(env.states, past_states(past))
        view = ScopedStates.build(overlay, visible_entities(agent, env.registry.last()))
        engine = TemplateEngine(
            view,
            fixed_clock(self._clock().tz, past.at),
            self._render_seconds,
            owner=agent.id,
        )
        variables: dict[str, Any] = {
            "source": past.source,
            "inputs": dict(past.inputs),
            "data": dict(past.data),
            "context": {},
        }
        try:
            matched = is_true(await engine.render_value(spec.match, variables))
        except TemplateError as err:
            case.detail = f"match: {err}"
            return case
        if not matched:
            case.outcome = "no_match"
            return case
        case.matched = True
        ctx = ToolContext(agent, invocation_id=run_id, dry_run=True)
        try:
            result = await self._result(
                spec,
                ctx=ctx,
                executor=executor,
                engine=engine,
                past=past,
                case=case,
                variables=variables,
                live_decisions=live_decisions,
            )
        except BudgetExceededError:
            raise
        except _Declined as err:
            case.outcome = "declined"
            case.detail = str(err)
            return case
        except SkipCase as err:
            case.outcome = "skipped"
            case.detail = str(err)
            return case
        except (RuleFailure, TemplateError, ScopeDeniedError) as err:
            code = getattr(err, "code", None)
            message = getattr(err, "message", None) or str(err)
            case.detail = f"{code}: {message}" if code else message
            return case
        except Exception as err:
            case.detail = f"{type(err).__name__}: {err}"
            return case
        case.rule_result = result
        return await self._compare(engine, spec, case, variables)

    async def _result(  # noqa: PLR0913
        self,
        spec: RuleSpec,
        *,
        ctx: ToolContext,
        executor: ToolExecutor,
        engine: TemplateEngine,
        past: PastInvocation,
        case: Case,
        variables: dict[str, Any],
        live_decisions: bool,
    ) -> Any:
        # The write checks of the live stage come first.
        check_writes(ctx, executor.env, spec)
        context = await self.context.collect(ctx, executor, spec.context, past)
        variables["context"] = context
        if isinstance(spec.action, TemplateAction):
            text = await engine.render(spec.action.template, variables)
            if text == DECLINE:
                msg = "the action declined"
                raise _Declined(msg)
            variables["output"] = parse_result(text)
        else:
            answers = await self._decide(
                ctx.agent, spec.action, variables, past, case, live=live_decisions
            )
            variables["output"] = answers
            variables["answers"] = answers
        if spec.result is not None:
            text = await engine.render(spec.result, variables)
            if text == DECLINE:
                msg = "the result declined"
                raise _Declined(msg)
            result = _json_value(parse_result(text))
        else:
            result = _json_value(variables["output"])
        variables["result"] = result
        for write in spec.writes:
            data = {
                k: _json_value(await engine.render_value(v, variables))
                for k, v in write.data.items()
            }
            await _check_write(ctx, executor, write.service, write.entity_id, data)
            case.writes.append(
                {"service": write.service, "entity_id": write.entity_id, "data": data}
            )
        return result

    async def _decide(  # noqa: PLR0913
        self,
        agent: Agent,
        action: DecisionAction,
        variables: Mapping[str, Any],
        past: PastInvocation,
        case: Case,
        *,
        live: bool,
    ) -> dict[str, Any]:
        ref = agent.definition.models.decision
        if ref is None:
            msg = "the definition has no decision model"
            raise RuleFailure(msg)
        request = DecisionRequest(
            model=DecisionModel(ref.provider, ref.model),
            values=decision_values(
                action.include,
                variables["inputs"],
                variables["data"],
                variables["context"],
            ),
            questions=[_question(q) for q in action.questions],
        )
        try:
            request.validate()
        except ValueError as err:
            raise RuleFailure(str(err)) from err
        recorded = past.decisions.get(request_key(request))
        if isinstance(recorded, Mapping):
            case.decision = "replayed"
            return dict(recorded)
        if not live:
            msg = "no recorded decision, and the live limit is reached"
            raise SkipCase(msg)
        case.decision = "live"
        result = await self._decisions().decide(request, agent_id=agent.id)
        return {
            q.name: answer_data(result.answers.get(q.name)) for q in action.questions
        }

    async def _compare(
        self,
        engine: TemplateEngine,
        spec: RuleSpec,
        case: Case,
        variables: dict[str, Any],
    ) -> Case:
        try:
            agreed = await agreement(
                engine, spec, variables, case.rule_result, case.agent_result
            )
        except TemplateError as err:
            case.outcome = "error"
            case.detail = f"agreement: {err}"
            return case
        case.outcome = "agreed" if agreed else "disagreed"
        return case


def _same_value(a: Any, b: Any) -> bool:
    return json.dumps(_json_value(a), sort_keys=True) == json.dumps(
        _json_value(b), sort_keys=True
    )


async def agreement(
    engine: TemplateEngine,
    spec: RuleSpec,
    variables: Mapping[str, Any],
    rule_result: Any,
    agent_result: Any,
) -> bool:
    """True if the two results agree (trials use it). Raise ``TemplateError``."""
    if spec.agreement is None:
        return _same_value(rule_result, agent_result)
    return is_true(
        await engine.render_value(
            spec.agreement,
            {**variables, "rule_result": rule_result, "agent_result": agent_result},
        )
    )
