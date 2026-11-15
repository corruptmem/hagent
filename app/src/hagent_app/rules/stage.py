"""The rule stage of the router (design sections 9.4 and 10).

``RuleEngine.try_rules`` runs for each invocation that a rule can answer:

1. Read the active rules of the agent once, in order.
2. Build the scoped state view once: every template of the invocation sees
   the same states (design section 13.2).
3. Render the match template of each rule in order. The first rule whose
   match is true runs. The other rules do not run.
4. If the rule has a cache and a fresh entry for the same key values, return
   the cached result. The action and the writes do not run.
5. Collect the context items (``rules/context.py``).
6. Run the action: render the template, or send one decision request.
7. Render the result template. ``decline`` passes the invocation to an
   agent turn.
8. Check every write again, then make each write through the tool executor,
   with no approval gate.
9. Record the outcome in the statistics of the rule version.

A rule that fails records an error, and the router starts an agent turn
(design section 18). A decision call that fails after the retries makes the
rule decline.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from hagent_app.agents import Agent
from hagent_app.providers.decisions import (
    ChoiceAnswer,
    ChoiceQuestion,
    DecisionClient,
    DecisionModel,
    DecisionRequest,
    Level,
    Question,
    Refusal,
    ScoreAnswer,
    ScoreQuestion,
    YesNoAnswer,
    YesNoQuestion,
)
from hagent_app.router import store as invocations
from hagent_app.router.retry import RetryPolicy, retryable
from hagent_app.router.rules import NO_RULE_MATCHED, RuleOutcome, RuleRequest
from hagent_app.rules.checks import check_writes
from hagent_app.rules.context import ContextCollector, RuleFailure
from hagent_app.rules.spec import (
    ChoiceSpec,
    DecisionAction,
    Include,
    RuleSpec,
    ScoreSpec,
    TemplateAction,
    YesNoSpec,
)
from hagent_app.rules.store import ActiveRule, RuleRun, RuleStore
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
from hagent_app.tools.base import StateView, ToolContext
from hagent_app.tools.executor import ToolExecutor
from hagent_app.tools.registry import RegistrySnapshot, RegistryUnavailableError
from hagent_app.tools.scope import ResolvedScope, ScopeDeniedError, agent_scope

_LOG = logging.getLogger(__name__)

type Sleep = Callable[[float], Awaitable[None]]


def visible_entities(
    agent: Agent, reg: RegistrySnapshot | None
) -> Callable[[str], bool]:
    """The entities that the agent's templates can read (design 10.2).

    - ``admin``: every entity.
    - ``none``: the input entities only.
    - ``read`` and ``control``: the read scope (the scope plus the inputs
      and the outputs), as the HA read tools use it.

    ``reg`` is the last known registry snapshot, so that rules keep working
    while HA restarts. Without a snapshot, a scope that uses devices, areas,
    floors or labels shrinks to its listed entities and domains.
    """
    definition = agent.definition
    level = definition.tools.ha.level
    if level == "admin":
        return lambda _entity_id: True
    inputs = set(definition.inputs.entities)
    if definition.inputs.devices and reg is not None:
        inputs |= reg.entities_of_devices(definition.inputs.devices)
    if level == "none":
        frozen = frozenset(inputs)
        return frozen.__contains__
    try:
        read = agent_scope(definition, reg).read
    except RegistryUnavailableError:
        scope = definition.tools.ha.scope
        read = ResolvedScope(
            all=scope.all,
            entities=frozenset(scope.entities)
            | frozenset(definition.outputs)
            | frozenset(inputs),
            domains=frozenset(scope.domains),
        )
    return read.contains


def _select(values: Mapping[str, Any], selection: bool | list[str]) -> dict[str, Any]:
    if selection is True:
        return dict(values)
    if selection is False:
        return {}
    return {k: values[k] for k in selection if k in values}


def _question(spec: YesNoSpec | ChoiceSpec | ScoreSpec) -> Question:
    if isinstance(spec, YesNoSpec):
        return YesNoQuestion(spec.name, spec.text, spec.yes, spec.no)
    if isinstance(spec, ChoiceSpec):
        return ChoiceQuestion(spec.name, spec.text, dict(spec.options))
    return ScoreQuestion(
        spec.name, spec.text, [Level(lv.label, lv.description) for lv in spec.levels]
    )


def answer_data(answer: Any) -> dict[str, Any]:
    """The template form of one decision answer."""
    if isinstance(answer, Refusal):
        return {"refused": True}
    if isinstance(answer, YesNoAnswer):
        return {"refused": False, "probability": answer.probability}
    if isinstance(answer, ChoiceAnswer):
        return {
            "refused": False,
            "choice": answer.choice,
            "probabilities": dict(answer.probabilities),
            "confidence": answer.confidence,
        }
    if isinstance(answer, ScoreAnswer):
        return {
            "refused": False,
            "score": answer.score,
            "probabilities": dict(answer.probabilities),
            "confidence": answer.confidence,
        }
    return {"refused": True}


def decision_values(
    include: Include,
    inputs: Mapping[str, Any],
    data: Mapping[str, Any],
    context: Mapping[str, Any],
) -> dict[str, Any]:
    """The values that every question of the decision request sees."""
    values: dict[str, Any] = {}
    if include.inputs is not False:
        values["inputs"] = _select(inputs, include.inputs)
    if include.data is not False:
        values["data"] = _select(data, include.data)
    if include.context is not False:
        values["context"] = _select(context, include.context)
    return values


class _Declined(Exception):  # noqa: N818 - a rule outcome
    """The rule declined."""


@dataclass(frozen=True, slots=True)
class _Run:
    """What one rule run uses."""

    request: RuleRequest
    engine: TemplateEngine
    ctx: ToolContext
    executor: ToolExecutor


@dataclass(frozen=True, slots=True)
class _Answer:
    result: Any
    cache_hit: bool = False


class RuleEngine:
    """The ``RuleStage`` of the router (task-13)."""

    def __init__(  # noqa: PLR0913
        self,
        store: RuleStore,
        db: Database,
        executor: Callable[[], ToolExecutor],
        decisions: Callable[[], DecisionClient],
        *,
        clock: Callable[[], TemplateClock] = TemplateClock,
        retry: RetryPolicy | None = None,
        render_seconds: float = 1.0,
        cache_entries: int = 1000,
        sleep: Sleep = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._db = db
        self._executor = executor
        self._decisions = decisions
        self._clock = clock
        self._retry = retry or RetryPolicy()
        self._render_seconds = render_seconds
        self._sleep = sleep
        self._monotonic = monotonic
        self._max_cache = cache_entries
        self._cache: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self.context = ContextCollector(db, max_entries=cache_entries, clock=monotonic)

    async def try_rules(self, request: RuleRequest) -> RuleOutcome:
        """Test the active rules in order. The first match answers."""
        agent = request.agent
        rules = await self._store.active_rules(agent.id)
        if not rules:
            return NO_RULE_MATCHED
        executor = self._executor().without_approval()
        env = executor.env
        view = self.scoped_states(agent, env.states, env.registry.last())
        engine = TemplateEngine(view, self._clock(), self._render_seconds)
        ctx = ToolContext(agent, invocation_id=request.invocation_id)
        variables: dict[str, Any] = {
            "source": request.request.source,
            "inputs": dict(request.inputs),
            "data": request.request.turn_data,
            "context": {},
        }
        for rule in rules:
            try:
                spec = RuleSpec.model_validate(rule.spec)
                matched = is_true(await engine.render_value(spec.match, variables))
            except (ValidationError, TemplateError) as err:
                return await self._failed(rule, f"match: {err}", matched=False)
            if matched:
                return await self._run(rule, spec, _Run(request, engine, ctx, executor))
        return NO_RULE_MATCHED

    @staticmethod
    def scoped_states(
        agent: Agent, states: StateView, reg: RegistrySnapshot | None
    ) -> ScopedStates:
        """The state view of the agent's templates."""
        return ScopedStates.build(states, visible_entities(agent, reg))

    async def _failed(
        self, rule: ActiveRule, error: str, *, matched: bool = True, cost: int = 0
    ) -> RuleOutcome:
        _LOG.info("rule %s v%d failed: %s", rule.id, rule.version, error)
        await self._store.record(
            rule.id, rule.version, RuleRun(matched=matched, error=error, cost_pusd=cost)
        )
        return RuleOutcome(
            answered=False,
            rule_id=rule.id,
            rule_version=rule.version,
            reason="rule_error",
        )

    async def _cost(self, invocation_id: str) -> int:
        return await self._db.run(lambda c: invocations.cost_pusd(c, invocation_id))

    async def _run(self, rule: ActiveRule, spec: RuleSpec, run: _Run) -> RuleOutcome:
        request = run.request
        before = await self._cost(request.invocation_id)
        try:
            answer = await self._answer(rule, spec, run)
        except asyncio.CancelledError:
            raise
        except _Declined as err:
            cost = await self._cost(request.invocation_id) - before
            await self._store.record(
                rule.id,
                rule.version,
                RuleRun(matched=True, declined=True, cost_pusd=cost),
            )
            _LOG.debug("rule %s v%d declined: %s", rule.id, rule.version, err)
            return RuleOutcome(
                answered=False,
                rule_id=rule.id,
                rule_version=rule.version,
                reason="declined",
            )
        except (RuleFailure, TemplateError, ScopeDeniedError) as err:
            cost = await self._cost(request.invocation_id) - before
            message = getattr(err, "message", None) or str(err)
            code = getattr(err, "code", None)
            return await self._failed(
                rule, f"{code}: {message}" if code else message, cost=cost
            )
        except Exception as err:
            _LOG.exception("rule %s v%d failed", rule.id, rule.version)
            cost = await self._cost(request.invocation_id) - before
            return await self._failed(rule, f"{type(err).__name__}: {err}", cost=cost)
        cost = await self._cost(request.invocation_id) - before
        await self._store.record(
            rule.id,
            rule.version,
            RuleRun(
                matched=True,
                answered=True,
                cache_hit=answer.cache_hit,
                cost_pusd=cost,
            ),
        )
        return RuleOutcome(
            answered=True,
            result=answer.result,
            rule_id=rule.id,
            rule_version=rule.version,
        )

    # ------------------------------------------------------------- one rule

    def _cache_key(self, rule: ActiveRule, spec: RuleSpec, request: RuleRequest) -> str:
        assert spec.cache is not None  # noqa: S101 - checked by the caller
        data = request.request.turn_data
        values = []
        for key in spec.cache.keys:
            if key.startswith("data."):
                values.append(data.get(key.removeprefix("data.")))
            else:
                values.append(request.inputs.get(key))
        return json.dumps(
            [rule.id, rule.version, request.request.source_key, values],
            sort_keys=True,
            default=str,
        )

    async def _answer(self, rule: ActiveRule, spec: RuleSpec, run: _Run) -> _Answer:
        request, engine, ctx, executor = run.request, run.engine, run.ctx, run.executor
        key = None
        if spec.cache is not None:
            key = self._cache_key(rule, spec, request)
            cached = self._cache.get(key)
            if cached is not None and cached[0] > self._monotonic():
                return _Answer(cached[1], cache_hit=True)

        # A rule whose writes now need approval, or left the scope, fails
        # before any part of it runs.
        check_writes(ctx, executor.env, spec)

        context = await self.context.collect(
            ctx, executor, spec.context, request.request.source_key
        )
        variables: dict[str, Any] = {
            "source": request.request.source,
            "inputs": dict(request.inputs),
            "data": request.request.turn_data,
            "context": context,
        }
        if isinstance(spec.action, TemplateAction):
            text = await engine.render(spec.action.template, variables)
            if text == DECLINE:
                msg = "the action declined"
                raise _Declined(msg)
            output: Any = parse_result(text)
            variables["output"] = output
        else:
            answers = await self._decide(request.agent, spec.action, variables)
            output = answers
            variables["output"] = answers
            variables["answers"] = answers

        if spec.result is not None:
            text = await engine.render(spec.result, variables)
            if text == DECLINE:
                msg = "the result declined"
                raise _Declined(msg)
            result = parse_result(text)
        else:
            result = output
        result = _json_value(result)

        variables["result"] = result
        for write in spec.writes:
            data = {
                k: _json_value(await engine.render_value(v, variables))
                for k, v in write.data.items()
            }
            domain, service = write.service.split(".", 1)
            outcome = await executor.execute(
                ctx,
                "ha.call_service",
                {
                    "domain": domain,
                    "service": service,
                    "target": {"entity_id": [write.entity_id]},
                    "data": data,
                },
            )
            if not outcome.ok:
                msg = (
                    f"write to {write.entity_id}: {outcome.error_code}: {outcome.error}"
                )
                raise RuleFailure(msg)

        if key is not None and spec.cache is not None:
            self._cache[key] = (self._monotonic() + spec.cache.seconds, result)
            self._cache.move_to_end(key)
            while len(self._cache) > self._max_cache:
                self._cache.popitem(last=False)
        return _Answer(result)

    async def _decide(
        self, agent: Agent, action: DecisionAction, variables: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Send one decision request. Decline if it fails after the retries."""
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
        attempt = 1
        while True:
            try:
                result = await self._decisions().decide(request, agent_id=agent.id)
                break
            except asyncio.CancelledError:
                raise
            except Exception as err:
                if attempt < self._retry.attempts and retryable(err):
                    await self._sleep(self._retry.delay(attempt))
                    attempt += 1
                    continue
                _LOG.warning("decision request failed: %s", type(err).__name__)
                msg = f"the decision request failed: {type(err).__name__}"
                raise _Declined(msg) from err
        return {
            q.name: answer_data(result.answers.get(q.name)) for q in action.questions
        }


def _json_value(value: Any) -> Any:
    """A JSON-compatible copy (tuples become lists, others become text)."""
    return json.loads(json.dumps(value, default=str))
