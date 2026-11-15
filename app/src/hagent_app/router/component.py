"""The router: one entry point for every invocation (design 9.4, 9.6, 9.7).

``RouterComponent.submit(request)`` handles one invocation in this order:

1. If the agent does not exist or is turned off, refuse it with an error.
2. Apply the concurrency mode of the agent to its queue (section 9.6).
3. When the invocation starts, read the agent snapshot. The invocation
   keeps it to the end, so that a change takes effect only between
   invocations. Collect the input values.
4. Ask the rule stage (task-13). Until task-13, no rule matches.
5. Run an agent turn. If the turn cannot give a result, return the
   fallback (section 9.7).
6. Record the path, the result, the cost, the definition version and the
   rule version in ``invocations``.

The ``invocations`` table is also the persistent queue. After a restart,
the router runs the queued invocations again, and records the running ones
as failed with the fallback.

Terms:

- **Lane**: the queue of one agent, or of one Assist conversation.
- **Active job**: a running invocation that nobody cancelled.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal, Protocol

from aiohttp import web

from hagent_app.agents import Agent, AgentDefinition, AgentReader
from hagent_app.agents.models import AgentNotFoundError
from hagent_app.http.errors import ApiError
from hagent_app.providers.meter import current_invocation
from hagent_app.providers.prices import from_pusd
from hagent_app.router import store
from hagent_app.router.api import RouterApi
from hagent_app.router.config import RouterConfig
from hagent_app.router.errors import RouterNotRunningError
from hagent_app.router.models import (
    AGENT_NOT_FOUND,
    AGENT_OFF,
    BUSY,
    CANCELLED,
    FALLBACK_ERROR,
    NO_LAST_RESULT,
    NO_RULE_SOURCES,
    NOT_RUNNING,
    QUEUE_FULL,
    FallbackReason,
    InvocationRequest,
    InvocationResult,
)
from hagent_app.router.overlay import StateOverlay, sample_state
from hagent_app.router.retry import RetryingModel, RetryPolicy
from hagent_app.router.rules import NoRules, RuleOutcome, RuleRequest, RuleStage
from hagent_app.turns.models import TurnInput, TurnRecord, TurnResult

if TYPE_CHECKING:
    from hagent_app.app import AppContext
    from hagent_app.ha import State
    from hagent_app.storage import Database
    from hagent_app.tools import StateView, ToolsComponent

_LOG = logging.getLogger(__name__)

type Mode = Literal["queued", "single", "restart", "parallel"]
type ResultListener = Callable[[InvocationResult], None]

# Stop reasons of an agent turn -> fallback reasons (design section 9.7).
_REASONS: dict[str, FallbackReason] = {
    "budget": "budget",
    "provider_error": "provider_unreachable",
    "tool_call_limit": "tool_call_limit",
    "token_limit": "token_limit",
    "request_limit": "request_limit",
    "invalid_result": "invalid_result",
    "error": "turn_failed",
}


class _History(Protocol):
    async def get(self, turn_id: str) -> TurnRecord | None: ...


class TurnsLike(Protocol):
    """What the router needs from ``TurnsComponent``."""

    @property
    def history(self) -> _History: ...

    async def run_turn(
        self, agent: Agent, turn: TurnInput, *, states: StateView | None = None
    ) -> TurnResult: ...


@dataclass(eq=False, slots=True)
class _Job:
    """One invocation in a lane, or one test invocation."""

    id: str
    request: InvocationRequest
    future: asyncio.Future[InvocationResult]
    task: asyncio.Task[None] | None = None
    # "cancelled" (restart mode or ``cancel``) or "app_stopped".
    cancel_reason: str | None = None
    agent: Agent | None = None
    dry_run: bool = False
    # A test invocation: the snapshot to use, and the sample states.
    agent_override: Agent | None = None
    samples: Mapping[str, State] = field(default_factory=dict)
    states: StateView | None = None
    turn: TurnResult | None = None


@dataclass(eq=False, slots=True)
class _Lane:
    key: str
    mode: Mode = "queued"
    running: set[_Job] = field(default_factory=set)
    waiting: deque[_Job] = field(default_factory=deque)

    @property
    def active(self) -> list[_Job]:
        return [j for j in self.running if j.cancel_reason is None]

    @property
    def busy(self) -> bool:
        return bool(self.active or self.waiting)


class InvocationHandle:
    """A submitted invocation. ``await handle.result()`` gives the result."""

    def __init__(self, invocation_id: str, future: asyncio.Future[InvocationResult]):
        self.invocation_id = invocation_id
        self._future = future

    def done(self) -> bool:
        """True when the invocation has ended."""
        return self._future.done()

    async def result(self) -> InvocationResult:
        """Wait for the result. A cancelled wait does not stop the invocation.

        Use ``RouterComponent.cancel`` to stop it (``on_timeout: cancel``).
        """
        return await asyncio.shield(self._future)


@dataclass(frozen=True, slots=True)
class TestOutcome:
    """The result of a test invocation and the record of its turn."""

    result: InvocationResult
    turn: TurnResult | None
    record: TurnRecord | None


class RetryingChatModels:
    """The chat model factory of ``TurnsComponent``, with retries.

    It wraps each ``MeteredModel`` in a ``RetryingModel``. The router sets
    ``policy`` from its options when it starts.
    """

    def __init__(self, factory: Callable[[Any, str], Any]) -> None:
        self._factory = factory
        self.policy = RetryPolicy()

    def __call__(self, ref: Any, agent_id: str) -> Any:
        return RetryingModel(self._factory(ref, agent_id), self.policy)


class RouterComponent:
    """The router. List it after ``turns`` in ``default_components``.

    Task-15 (scheduler) and task-17 (``hagent.invoke``, AI Task, Assist)
    call ``submit`` or ``invoke``. Task-13 sets ``rules`` before the app
    starts.
    """

    name = "router"

    def __init__(
        self,
        turns: TurnsLike,
        *,
        tools: ToolsComponent | None = None,
        rules: RuleStage | None = None,
        models: RetryingChatModels | None = None,
    ) -> None:
        self._turns = turns
        self._tools = tools
        self.rules: RuleStage = rules or NoRules()
        self._models = models
        self._ctx: AppContext | None = None
        self._reader: AgentReader | None = None
        self._config = RouterConfig()
        self._lanes: dict[str, _Lane] = {}
        self._free: set[_Job] = set()
        self._lock = asyncio.Lock()
        self._listeners: list[ResultListener] = []
        self._stopping = False

    # ------------------------------------------------------------ lifecycle

    async def start(self, ctx: AppContext) -> None:
        """Recover the queue of the last run, then accept invocations."""
        self._config = ctx.config.section("router", RouterConfig)
        if self._models is not None:
            self._models.policy = RetryPolicy(
                attempts=self._config.retry_attempts,
                base_delay=self._config.retry_base_delay,
                max_delay=self._config.retry_max_delay,
            )
        self._ctx = ctx
        self._reader = AgentReader(ctx.db)
        self._lock = asyncio.Lock()
        self._stopping = False
        await self._recover()

    async def stop(self) -> None:
        """Stop every running invocation and keep the queued ones.

        A running invocation records the fallback with the reason
        ``app_stopped``. A queued invocation stays in the table and runs
        after the next start. Its caller receives ``not_running``.
        """
        async with self._lock:
            self._stopping = True
        jobs = [j for lane in self._lanes.values() for j in lane.running]
        jobs += list(self._free)
        for job in jobs:
            if job.task is not None and not job.task.done():
                job.cancel_reason = job.cancel_reason or "app_stopped"
                job.task.cancel()
        tasks = [j.task for j in jobs if j.task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for lane in self._lanes.values():
            for job in lane.waiting:
                if not job.future.done():
                    job.future.set_result(
                        self._refusal(job, NOT_RUNNING, "the app stops; it runs later")
                    )
        self._lanes.clear()
        self._free.clear()
        self._ctx = None

    # ------------------------------------------------------------ interface

    def routes(self) -> list[web.RouteDef]:
        """The routes for ``HttpServer.add_routes``: the test invocation."""
        return RouterApi(self._running).routes()

    def _running(self) -> RouterComponent:
        if self._ctx is None or self._stopping:
            raise ApiError(503, "not_ready", "the agent runtime is not running")
        return self

    def add_result_listener(self, listener: ResultListener) -> None:
        """Call ``listener`` with each final result, also of resumed runs.

        Task-17 uses it to fire ``hagent_result`` for invocations that no
        caller waits for. ``listener`` is a plain function. It must be fast.
        """
        self._listeners.append(listener)

    async def invoke(self, request: InvocationRequest) -> InvocationResult:
        """Submit the invocation and wait for its result."""
        handle = await self.submit(request)
        return await handle.result()

    async def submit(self, request: InvocationRequest) -> InvocationHandle:
        """Accept one invocation and return its handle at once."""
        if self._ctx is None:
            msg = "the router is not running"
            raise RouterNotRunningError(msg)
        db = self._ctx.db
        job = _Job(
            uuid.uuid4().hex, request, asyncio.get_running_loop().create_future()
        )
        handle = InvocationHandle(job.id, job.future)
        async with self._lock:
            if self._stopping:
                msg = "the router is not running"
                raise RouterNotRunningError(msg)
            agent = await self._agents().get(request.agent_id)
            if agent is None:
                job.future.set_result(
                    self._refusal(job, AGENT_NOT_FOUND, "the agent does not exist")
                )
                return handle
            if not agent.enabled:
                await self._refuse(job, AGENT_OFF, "the agent is turned off")
                return handle
            lane = self._lanes.setdefault(request.queue_key, _Lane(request.queue_key))
            mode, size = self._mode(agent, request)
            lane.mode = mode
            if mode == "single" and lane.busy:
                await self._refuse(job, BUSY, "the agent is busy (mode single)")
                return handle
            if mode == "queued" and lane.busy and len(lane.waiting) >= size:
                await self._refuse(job, QUEUE_FULL, f"the queue holds {size}")
                return handle
            if mode == "parallel" and len(lane.active) >= size:
                await self._refuse(job, QUEUE_FULL, f"{size} invocations run")
                return handle
            await db.run(lambda c: store.insert(c, job.id, request, status="queued"))
            # A job of this lane can end during the insert, and its done
            # callback then removes the empty lane. Only ``submit`` creates
            # lanes, under the lock, so the same lane object goes back.
            self._lanes[request.queue_key] = lane
            if mode == "restart":
                for other in [*lane.running, *lane.waiting]:
                    self._cancel_job(lane, other, "cancelled")
            lane.waiting.append(job)
            self._pump(lane)
        return handle

    async def cancel(self, invocation_id: str) -> bool:
        """Cancel a queued or running invocation. Return false if unknown.

        The invocation records ``cancelled`` and writes nothing more.
        """
        for lane in self._lanes.values():
            for job in [*lane.running, *lane.waiting]:
                if job.id == invocation_id and job.cancel_reason is None:
                    self._cancel_job(lane, job, "cancelled")
                    self._pump(lane)
                    return True
        return False

    async def get(self, invocation_id: str) -> dict[str, Any] | None:
        """One invocation record, or ``None``."""
        db = self._db()
        return await db.run(lambda c: store.get(c, invocation_id))

    async def test(
        self,
        agent_id: str,
        *,
        definition: AgentDefinition | None = None,
        text: str | None = None,
        data: Mapping[str, Any] | None = None,
        states: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> TestOutcome:
        """Run a test invocation: one dry-run agent turn with sample data.

        The test does not use the queue, so it never waits for, or cancels,
        real work. Tools check the scope as usual, but do not change HA or a
        store. ``states`` replace the state cache for this turn.
        """
        if self._ctx is None or self._stopping:
            msg = "the router is not running"
            raise RouterNotRunningError(msg)
        reader = self._agents()
        if definition is not None:
            agent = await reader.check_draft(agent_id, definition)
            agent = replace(agent, definition=definition)
        else:
            found = await reader.get(agent_id)
            if found is None:
                msg = f"no agent with ID {agent_id!r}"
                raise AgentNotFoundError(msg)
            agent = found
        # A draft test ran a definition that no version holds. The marker
        # tells history readers that agent_version is only the base.
        caller = {"draft": True} if definition is not None else {}
        request = InvocationRequest(
            agent_id, "test", text=text, data=data or {}, caller=caller
        )
        samples = {
            eid: sample_state(eid, value) for eid, value in (states or {}).items()
        }
        job = _Job(
            uuid.uuid4().hex,
            request,
            asyncio.get_running_loop().create_future(),
            dry_run=True,
            agent_override=agent,
            samples=samples,
            states=StateOverlay(self._ctx.states, samples) if samples else None,
        )
        await self._db().run(
            lambda c: store.insert(c, job.id, request, status="queued", dry_run=True)
        )
        self._free.add(job)
        job.task = asyncio.create_task(self._run(job))
        job.task.add_done_callback(lambda _t: self._free.discard(job))
        result = await asyncio.shield(job.future)
        record = None
        if job.turn is not None:
            record = await self._turns.history.get(job.turn.turn_id)
        return TestOutcome(result, job.turn, record)

    # ------------------------------------------------------------ lanes

    def _mode(self, agent: Agent, request: InvocationRequest) -> tuple[Mode, int]:
        if request.conversation_id:
            # Assist runs the messages of one conversation in order.
            return "queued", self._config.assist_queue_size
        concurrency = agent.definition.concurrency
        return concurrency.mode, concurrency.queue_size

    def _pump(self, lane: _Lane) -> None:
        """Start waiting jobs while the mode permits it."""
        if self._stopping:
            return
        while lane.waiting and (lane.mode == "parallel" or not lane.active):
            self._start(lane, lane.waiting.popleft())

    def _start(self, lane: _Lane, job: _Job) -> None:
        lane.running.add(job)
        job.task = asyncio.create_task(self._run(job), name=f"invocation-{job.id}")
        job.task.add_done_callback(lambda _t: self._done(lane, job))

    def _done(self, lane: _Lane, job: _Job) -> None:
        lane.running.discard(job)
        if self._stopping:
            return
        self._pump(lane)
        if not lane.running and not lane.waiting:
            self._lanes.pop(lane.key, None)

    def _cancel_job(self, lane: _Lane, job: _Job, reason: str) -> None:
        job.cancel_reason = reason
        if job in lane.waiting:
            lane.waiting.remove(job)
            # The job records "cancelled" in its own task.
            self._start(lane, job)
        elif job.task is not None:
            job.task.cancel()

    # ------------------------------------------------------------ one run

    async def _run(self, job: _Job) -> None:
        # Each task has its own context, so the value stays in this run and
        # in the tasks that it starts (for example the summary update).
        current_invocation.set(job.id)
        try:
            if job.cancel_reason is not None:
                result = await self._cancelled(job)
            else:
                result = await self._execute(job)
        except asyncio.CancelledError:
            if job.cancel_reason == "app_stopped":
                result = await asyncio.shield(self._fallback(job, "app_stopped"))
            else:
                result = await asyncio.shield(self._cancelled(job))
        except Exception as err:
            _LOG.exception("invocation %s failed", job.id)
            result = await asyncio.shield(
                self._fallback(job, "runtime_unavailable", error=repr(err))
            )
        self._deliver(job, result)

    async def _execute(self, job: _Job) -> InvocationResult:
        request = job.request
        agent = job.agent_override or await self._agents().get(request.agent_id)
        if agent is None:
            return await self._finish(
                self._refusal(job, AGENT_NOT_FOUND, "the agent does not exist")
            )
        if not agent.enabled and not job.dry_run:
            return await self._finish(
                self._refusal(job, AGENT_OFF, "the agent is turned off")
            )
        job.agent = agent
        await self._db().run(lambda c: store.mark_running(c, job.id, agent.version))
        inputs = await self._inputs(agent, job.samples)

        outcome: RuleOutcome | None = None
        if request.source not in NO_RULE_SOURCES:
            try:
                outcome = await self.rules.try_rules(
                    RuleRequest(job.id, agent, request, inputs)
                )
            except Exception:
                _LOG.exception("the rule stage failed for invocation %s", job.id)
                outcome = RuleOutcome(answered=False, reason="rule_error")
            if outcome.answered:
                return await self._finish(
                    self._result(
                        job,
                        "ok",
                        path="rule",
                        result=outcome.result,
                        rule_id=outcome.rule_id,
                        rule_version=outcome.rule_version,
                    )
                )

        rule_id = outcome.rule_id if outcome else None
        rule_version = outcome.rule_version if outcome else None
        try:
            turn = await self._turns.run_turn(
                agent,
                TurnInput(
                    source=request.source,
                    inputs=inputs,
                    data=request.turn_data,
                    invocation_id=job.id,
                    conversation_id=request.conversation_id,
                    result_schema=request.result_schema,
                    dry_run=job.dry_run,
                ),
                states=job.states,
            )
        except asyncio.CancelledError:
            raise
        except Exception as err:
            _LOG.exception("cannot run an agent turn for invocation %s", job.id)
            return await self._fallback(
                job,
                "runtime_unavailable",
                error=f"{type(err).__name__}: {err}",
                rule_id=rule_id,
                rule_version=rule_version,
            )
        job.turn = turn
        if turn.ok:
            return await self._finish(
                self._result(
                    job,
                    "ok",
                    path="agent_turn",
                    result=turn.result,
                    turn_id=turn.turn_id,
                    rule_id=rule_id,
                    rule_version=rule_version,
                )
            )
        reason = _REASONS.get(turn.stop_reason or "error", "turn_failed")
        return await self._fallback(
            job,
            reason,
            error=turn.error,
            turn_id=turn.turn_id,
            rule_id=rule_id,
            rule_version=rule_version,
        )

    async def _inputs(
        self, agent: Agent, samples: Mapping[str, State]
    ) -> dict[str, Any]:
        """The current values of the input entities (design section 9.4)."""
        ctx = self._ctx
        assert ctx is not None  # noqa: S101 - only called while running
        if not ctx.states.ready:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(self._config.state_wait_seconds):
                    await ctx.states.wait_ready()
        ids = list(agent.definition.inputs.entities)
        ids += self._device_entities(agent.definition.inputs.devices)
        values: dict[str, Any] = {}
        for entity_id in [*ids, *samples]:
            state = samples.get(entity_id) or ctx.states.get(entity_id)
            values[entity_id] = None if state is None else state.state
        return values

    def _device_entities(self, devices: Iterable[str]) -> list[str]:
        devices = list(devices)
        if not devices or self._tools is None or self._tools.registry is None:
            return []
        snapshot = self._tools.registry.last()
        if snapshot is None:
            return []
        found: set[str] = set()
        for device_id in devices:
            found |= snapshot.all_entities_of_device(device_id)
        return sorted(found)

    # ------------------------------------------------------------ results

    def _result(self, job: _Job, status: Any, **fields: Any) -> InvocationResult:
        agent = job.agent
        return InvocationResult(
            invocation_id=job.id,
            agent_id=job.request.agent_id,
            status=status,
            agent_version=None if agent is None else agent.version,
            dry_run=job.dry_run,
            **fields,
        )

    def _refusal(self, job: _Job, code: str, message: str) -> InvocationResult:
        return self._result(job, "error", error_code=code, error=message)

    async def _refuse(self, job: _Job, code: str, message: str) -> None:
        """Record and return an invocation that never runs."""
        result = self._refusal(job, code, message)
        await self._db().run(lambda c: store.insert_finished(c, job.request, result))
        self._deliver(job, result)

    async def _cancelled(self, job: _Job) -> InvocationResult:
        return await self._finish(
            self._result(
                job,
                "cancelled",
                error_code=CANCELLED,
                error="the invocation was cancelled",
                turn_id=None if job.turn is None else job.turn.turn_id,
            )
        )

    async def _fallback(
        self, job: _Job, reason: FallbackReason, **fields: Any
    ) -> InvocationResult:
        agent = job.agent or job.agent_override
        if agent is None:
            agent = await self._agents().get(job.request.agent_id)
        definition = None if agent is None else agent.definition
        result = await fallback_result(
            self._db(),
            self._result(job, "fallback", **fields),
            job.request,
            definition,
            reason,
        )
        return await self._finish(result)

    async def _finish(self, result: InvocationResult) -> InvocationResult:
        db = self._db()
        pusd = await db.run(lambda c: store.cost_pusd(c, result.invocation_id))
        result = replace(result, cost_usd=from_pusd(pusd))
        await db.run(lambda c: store.finish(c, result))
        return result

    def _deliver(self, job: _Job, result: InvocationResult) -> None:
        if not job.future.done():
            job.future.set_result(result)
        for listener in list(self._listeners):
            try:
                listener(result)
            except Exception:
                _LOG.exception("a result listener failed")

    # ------------------------------------------------------------ recovery

    async def _recover(self) -> None:
        """Run the queue of the last app run again (design section 9.6)."""
        db = self._db()
        rows = await db.run(store.pending)
        reader = self._agents()
        resumed = 0
        for row in rows:
            if row.status == "running" or row.dry_run:
                version = (
                    None
                    if row.agent_version is None
                    else await reader.get_version(
                        row.request.agent_id, row.agent_version
                    )
                )
                base = InvocationResult(
                    invocation_id=row.id,
                    agent_id=row.request.agent_id,
                    status="fallback",
                    agent_version=row.agent_version,
                    dry_run=row.dry_run,
                )
                result = await fallback_result(
                    db,
                    base,
                    row.request,
                    None if version is None else version.definition,
                    "app_restart",
                )
                result = await self._finish(result)
                for listener in list(self._listeners):
                    try:
                        listener(result)
                    except Exception:
                        _LOG.exception("a result listener failed")
                continue
            job = _Job(row.id, row.request, asyncio.get_running_loop().create_future())
            lane = self._lanes.setdefault(
                row.request.queue_key, _Lane(row.request.queue_key)
            )
            lane.waiting.append(job)
            resumed += 1
        if rows:
            _LOG.info(
                "router: %d invocations resumed, %d recorded as failed",
                resumed,
                len(rows) - resumed,
            )
        for lane in list(self._lanes.values()):
            self._pump(lane)

    # ------------------------------------------------------------ helpers

    def _db(self) -> Database:
        if self._ctx is None:
            msg = "the router is not running"
            raise RouterNotRunningError(msg)
        return self._ctx.db

    def _agents(self) -> AgentReader:
        if self._reader is None:
            msg = "the router is not running"
            raise RouterNotRunningError(msg)
        return self._reader


async def fallback_result(
    db: Database,
    base: InvocationResult,
    request: InvocationRequest,
    definition: AgentDefinition | None,
    reason: FallbackReason,
) -> InvocationResult:
    """Apply the fallback of the definition (design section 9.7).

    ``base`` holds the fields that the router already knows (turn, rule,
    version). The result has the path ``fallback`` and the reason.
    """
    base = replace(base, path="fallback", fallback_reason=reason, result=None)
    mode = "error" if definition is None else definition.fallback.mode
    message = f"the agent could not answer ({reason})"
    if base.error:
        message += f": {base.error}"
    if mode == "last_result":
        found, value = await db.run(
            lambda c: store.last_result(c, request.agent_id, request.source_key)
        )
        if found:
            return replace(base, status="fallback", result=value)
        return replace(
            base,
            status="error",
            error_code=NO_LAST_RESULT,
            error=f"{message}; no earlier result exists",
        )
    if mode == "fixed":
        assert definition is not None  # noqa: S101 - mode comes from it
        return replace(base, status="fallback", result=definition.fallback.value)
    if mode == "no_change":
        return replace(base, status="fallback", result=None)
    return replace(base, status="error", error_code=FALLBACK_ERROR, error=message)


__all__ = [
    "InvocationHandle",
    "RetryingChatModels",
    "RouterComponent",
    "RouterNotRunningError",
    "TestOutcome",
    "TurnsLike",
    "fallback_result",
]
