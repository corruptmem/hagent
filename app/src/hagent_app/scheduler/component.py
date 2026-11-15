"""The scheduler: timers, cron schedules, reviews and event subscriptions.

Design sections 12 and 14. Terms:

- **Schedule**: a timer (one invocation at a time) or a cron schedule that
  an agent creates with the ``self`` tools. It invokes the agent with the
  source ``schedule``, or ``review`` if the spec sets ``review``.
- **Review schedule**: the ``review`` cron of the definition. It invokes the
  agent with the source ``review``.
- **Subscription**: a filter on HA events or state changes that an agent
  creates with the ``self`` tools. A match invokes the agent with the source
  ``event``.
- **Job**: one APScheduler entry. The job ID is ``item:<item ID>`` for a
  schedule, and ``review:<agent ID>`` for the review schedule.
- **Own write**: a state change that a service call of the subscriber
  caused. ``OwnWriteTracker`` finds it through the HA context ID.

APScheduler 3 runs the jobs. Its SQLAlchemy job store keeps the jobs in a
separate SQLite file. The table ``scheduler_items`` is the source of truth:
on each start, the component makes the job store match it. The component
applies the missed-run policy itself before APScheduler starts, because
APScheduler reads the wall clock directly and a test cannot replace it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from typing import TYPE_CHECKING, Any, Protocol
from zoneinfo import ZoneInfo

import tzlocal
from apscheduler.events import EVENT_JOB_MISSED, JobExecutionEvent
from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.job import Job
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.date import DateTrigger

from hagent_app.agents import Agent, AgentReader
from hagent_app.http.events import ChangeEvent, ChangeEvents
from hagent_app.own_writes import Classified, OwnWriteTracker
from hagent_app.router.models import InvocationRequest, InvocationResult
from hagent_app.scheduler import store
from hagent_app.scheduler.config import SchedulerConfig
from hagent_app.scheduler.cron import cron_trigger, min_gap_seconds
from hagent_app.scheduler.redact import redact
from hagent_app.tools.base import ToolContext, ToolEnv
from hagent_app.tools.scope import ScopeDeniedError
from hagent_app.tools.store_tools import (
    DENIED_EVENT_TYPES,
    MAX_TIMER_DELAY,
    SPEC_CHECKS,
)
from hagent_app.tools.stores import AgentStateStore, StateKind, StoreError

if TYPE_CHECKING:
    from hagent_app.app import AppContext
    from hagent_app.ha.client import Subscription
    from hagent_app.storage import Database
    from hagent_app.tools import ToolsComponent

_LOG = logging.getLogger(__name__)

type Clock = Callable[[], datetime]
type Payload = tuple[Mapping[str, Any], str]

JOB_FUNC = "hagent_app.scheduler.component:fire_job"
TICK_SECONDS = 0.5
PRUNE_SECONDS = 3600.0
_CATCH_UP_LIMIT = 100_000

# Running components by installation ID. APScheduler stores each job with a
# text reference to ``fire_job`` and plain keyword arguments, so a job finds
# its component through this map after a restart. Tests run several apps in
# one process, each with its own installation ID.
_RUNNING: dict[str, SchedulerComponent] = {}


async def fire_job(key: str, job_id: str) -> None:
    """The APScheduler job function: fire one job of one component."""
    component = _RUNNING.get(key)
    if component is None:
        _LOG.warning("job %s fired with no running scheduler", job_id)
        return
    await component.fire(job_id)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


class Handle(Protocol):
    """The handle of a submitted invocation (``InvocationHandle``)."""

    @property
    def invocation_id(self) -> str:
        """The invocation ID."""
        ...

    async def result(self) -> InvocationResult:
        """The final result."""
        ...


class Router(Protocol):
    """The router entry point (``RouterComponent.submit``)."""

    async def submit(self, request: InvocationRequest) -> Any:
        """Accept one invocation and return its handle."""
        ...


@dataclass(slots=True)
class _Sub:
    """One subscription in the in-memory index."""

    item_id: str
    agent_id: str
    spec: dict[str, Any]
    event_type: str
    entity_ids: frozenset[str]
    include_own_writes: bool
    debounce: float
    pending: list[Classified[Payload]] = field(default_factory=list)
    timer: asyncio.TimerHandle | None = None


class _CallObserver:
    """The ``ServiceCallObserver`` of the tool executor."""

    def __init__(self, component: SchedulerComponent) -> None:
        self._component = component

    def call_started(self, agent_id: str) -> int:
        tracker = self._component.tracker
        return 0 if tracker is None else tracker.call_started(agent_id)

    def call_finished(self, token: int, context_id: str | None) -> None:
        tracker = self._component.tracker
        if tracker is None or token == 0:
            return
        try:
            released = tracker.call_finished(token, context_id)
        except KeyError:
            return  # The tracker restarted during the call.
        self._component.handle_classified(released)


class SchedulerStateStore:
    """The ``AgentStateStore`` of the ``self`` tools.

    It holds schedules and subscriptions. Rules belong to task-13: set
    ``rules`` to its store. Until then, rule calls fail with
    ``not_supported``.
    """

    def __init__(self, component: SchedulerComponent) -> None:
        self._component = component
        self.rules: AgentStateStore | None = None

    def _rules(self) -> AgentStateStore:
        if self.rules is None:
            raise StoreError("not_supported", "rules are not supported yet")
        return self.rules

    async def owner(self, kind: StateKind, item_id: str) -> str | None:
        if kind == "rule":
            # The tools call owner() inside the scope check. With no rule
            # store, no agent owns a rule: the tools return not_owner.
            if self.rules is None:
                return None
            return await self.rules.owner(kind, item_id)
        item = await self._component.get_item(item_id)
        return item.agent_id if item is not None and item.kind == kind else None

    async def list_items(self, agent_id: str, kind: StateKind) -> list[dict[str, Any]]:
        if kind == "rule":
            return await self._rules().list_items(agent_id, kind)
        return await self._component.list_public(agent_id, kind)

    async def create(
        self, agent_id: str, kind: StateKind, spec: Mapping[str, Any]
    ) -> dict[str, Any]:
        if kind == "rule":
            return await self._rules().create(agent_id, kind, spec)
        return await self._component.create_item(agent_id, kind, spec)

    async def update(
        self, agent_id: str, kind: StateKind, item_id: str, spec: Mapping[str, Any]
    ) -> dict[str, Any]:
        if kind == "rule":
            return await self._rules().update(agent_id, kind, item_id, spec)
        return await self._component.update_item(agent_id, kind, item_id, spec)

    async def set_enabled(
        self, agent_id: str, kind: StateKind, item_id: str, enabled: bool
    ) -> None:
        if kind == "rule":
            await self._rules().set_enabled(agent_id, kind, item_id, enabled)
            return
        await self._component.set_item_enabled(agent_id, kind, item_id, enabled)

    async def delete(self, agent_id: str, kind: StateKind, item_id: str) -> None:
        if kind == "rule":
            await self._rules().delete(agent_id, kind, item_id)
            return
        await self._component.delete_item(agent_id, kind, item_id)


class SchedulerComponent:
    """Runs schedules, reviews and subscriptions (design sections 12, 14).

    ``default_components()`` sets ``tools.agent_state`` to ``state_store``
    and ``tools.calls`` to ``calls``. Start the component after the router:
    the missed-run policy submits invocations during ``start``.
    """

    name = "scheduler"

    def __init__(
        self,
        router: Router,
        tools: ToolsComponent | None = None,
        events: ChangeEvents | None = None,
        *,
        clock: Clock = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._router = router
        self._tools = tools
        self._events = events
        self._clock = clock
        self._monotonic = monotonic
        self.state_store = SchedulerStateStore(self)
        self.calls = _CallObserver(self)
        self.tracker: OwnWriteTracker[Payload] | None = None
        self._ctx: AppContext | None = None
        self._config = SchedulerConfig()
        self._tz: tzinfo = UTC
        self._key = ""
        self._scheduler: AsyncIOScheduler | None = None
        self._agents: dict[str, Agent] = {}
        self._subs: dict[str, _Sub] = {}
        self._by_entity: dict[str, set[str]] = {}
        self._ha_subs: dict[str, Subscription] = {}
        self._ha_lock = asyncio.Lock()
        self._queue: asyncio.Queue[Mapping[str, Any]] = asyncio.Queue()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._loops: list[asyncio.Task[None]] = []
        self._unsubscribe: Callable[[], None] | None = None

    # ------------------------------------------------------------ life

    async def start(self, ctx: AppContext) -> None:
        """Restore the jobs and subscriptions, and apply the missed-run policy."""
        self._ctx = ctx
        self._config = ctx.config.section("scheduler", SchedulerConfig)
        self._tz = (
            ZoneInfo(self._config.timezone)
            if self._config.timezone
            else tzlocal.get_localzone()
        )
        self._key = ctx.installation_id
        self.tracker = OwnWriteTracker(
            hold=self._config.own_write_hold_seconds,
            ttl=self._config.own_write_ttl_seconds,
            clock=self._monotonic,
        )
        url = f"sqlite:///{ctx.config.data_dir / self._config.job_store}"
        self._scheduler = AsyncIOScheduler(
            jobstores={"default": SQLAlchemyJobStore(url=url)},
            executors={"default": AsyncIOExecutor()},
            job_defaults={
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": int(self._config.missed_run_grace_seconds),
            },
            timezone=self._tz,
        )
        _RUNNING[self._key] = self
        self._scheduler.add_listener(self._on_missed, EVENT_JOB_MISSED)
        # Paused: the job store is open, but no job runs before the catch-up.
        self._scheduler.start(paused=True)
        await self._prune()
        agents = await self._reader.list_agents()
        self._agents = {a.id: a for a in agents}
        await self._reconcile()
        await self._catch_up()
        self._scheduler.resume()
        for item in await self._db.run(
            lambda c: store.list_items(c, kind="subscription")
        ):
            if item.enabled:
                self._index(item)
        await self._sync_ha()
        self._loops = [
            asyncio.create_task(self._consume()),
            asyncio.create_task(self._tick()),
        ]
        if self._events is not None:
            self._unsubscribe = self._events.subscribe(self._on_change)

    async def stop(self) -> None:
        """Stop the jobs and the subscriptions."""
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        for task in [*self._loops, *self._tasks]:
            task.cancel()
        for task in [*self._loops, *self._tasks]:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._loops = []
        self._tasks.clear()
        for sub in self._subs.values():
            if sub.timer is not None:
                sub.timer.cancel()
        self._subs.clear()
        self._by_entity.clear()
        if self._ctx is not None:
            for ha_sub in self._ha_subs.values():
                with contextlib.suppress(Exception):
                    await self._ctx.ha.unsubscribe(ha_sub)
        self._ha_subs.clear()
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
        if _RUNNING.get(self._key) is self:
            del _RUNNING[self._key]
        self.tracker = None
        self._ctx = None

    @property
    def _db(self) -> Database:
        if self._ctx is None:
            raise StoreError("unavailable", "the scheduler is not running")
        return self._ctx.db

    @property
    def _reader(self) -> AgentReader:
        return AgentReader(self._db)

    @property
    def _jobs(self) -> AsyncIOScheduler:
        if self._scheduler is None:
            raise StoreError("unavailable", "the scheduler is not running")
        return self._scheduler

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            _LOG.error("scheduler task failed", exc_info=task.exception())

    # ------------------------------------------------------------ jobs

    def _trigger(self, item: store.Item) -> tuple[BaseTrigger, str]:
        spec = item.spec
        if spec["type"] == "timer":
            assert item.run_at is not None  # noqa: S101 - set for each timer
            return DateTrigger(run_date=_parse(item.run_at)), f"timer {item.run_at}"
        return cron_trigger(spec["cron"], self._tz), f"cron {spec['cron']} {self._tz}"

    def _review_trigger(self, agent: Agent) -> tuple[BaseTrigger, str]:
        cron = agent.definition.review.cron
        return cron_trigger(cron, self._tz), f"review {cron} {self._tz}"

    def _wanted(self, items: list[store.Item]) -> dict[str, tuple[BaseTrigger, str]]:
        wanted: dict[str, tuple[BaseTrigger, str]] = {}
        for item in items:
            if item.enabled:
                wanted[f"item:{item.id}"] = self._trigger(item)
        for agent in self._agents.values():
            if agent.definition.review.enabled:
                wanted[f"review:{agent.id}"] = self._review_trigger(agent)
        return wanted

    def _put_job(self, job_id: str, trigger: BaseTrigger, signature: str) -> None:
        """Add a job, or replace it if its trigger changed."""
        scheduler = self._jobs
        job = scheduler.get_job(job_id)
        if job is not None and job.name == signature:
            return
        scheduler.add_job(
            JOB_FUNC,
            trigger=trigger,
            id=job_id,
            name=signature,
            kwargs={"key": self._key, "job_id": job_id},
            replace_existing=True,
        )

    def _drop_job(self, job_id: str) -> None:
        if self._scheduler is not None and self._scheduler.get_job(job_id):
            self._scheduler.remove_job(job_id)

    async def _reconcile(self) -> None:
        """Make the job store match ``scheduler_items`` and the definitions."""
        items = await self._db.run(lambda c: store.list_items(c, kind="schedule"))
        wanted = self._wanted(items)
        for job in self._jobs.get_jobs():
            if job.id not in wanted:
                self._jobs.remove_job(job.id)
        for job_id, (trigger, signature) in wanted.items():
            self._put_job(job_id, trigger, signature)

    async def _catch_up(self) -> None:
        """Apply the missed-run policy of design section 12.

        A job that was due while the app was stopped runs once if its last
        due time is less than ``missed_run_grace_seconds`` late. Otherwise
        the component records a skip. Several missed due times of one job
        count as one run.
        """
        now = self._clock()
        grace = self._config.missed_run_grace_seconds
        for job in self._jobs.get_jobs():
            due: datetime | None = job.next_run_time
            if due is None or due > now:
                continue
            latest, upcoming, count = self._missed(job, due, now)
            late = (now - latest).total_seconds()
            if late < grace:
                await self.fire(job.id, due=latest, missed_count=count)
            else:
                await self._skip_missed(job.id, latest, count)
            current = self._jobs.get_job(job.id)
            if current is None:
                continue
            if upcoming is None:
                self._jobs.remove_job(job.id)
                await self._timer_done(job.id)
            else:
                current.modify(next_run_time=upcoming)

    @staticmethod
    def _missed(
        job: Job, due: datetime, now: datetime
    ) -> tuple[datetime, datetime | None, int]:
        """The last missed due time, the next future due time, and the count."""
        latest, count = due, 1
        upcoming: datetime | None = None
        while count < _CATCH_UP_LIMIT:
            upcoming = job.trigger.get_next_fire_time(
                latest, latest + timedelta(microseconds=1)
            )
            if upcoming is None or upcoming > now:
                break
            latest, count = upcoming, count + 1
        else:
            upcoming = job.trigger.get_next_fire_time(latest, now)
        return latest, upcoming, count

    async def _skip_missed(self, job_id: str, due: datetime, count: int) -> None:
        owner = await self._job_owner(job_id)
        if owner is None:
            return
        agent_id, item_id, source = owner
        at = _iso(self._clock())
        await self._db.run(
            lambda c: store.insert_run(
                c,
                agent_id=agent_id,
                item_id=item_id,
                source=source,
                due_at=_iso(due),
                at=at,
                status="skipped_missed",
                missed_count=count,
            )
        )
        _LOG.info("skipped job %s: %d missed due times", job_id, count)

    async def _job_owner(self, job_id: str) -> tuple[str, str | None, str] | None:
        kind, _, ident = job_id.partition(":")
        if kind == "review":
            return ident, None, "review"
        item = await self._db.run(lambda c: store.get_item(c, ident))
        if item is None:
            return None
        source = "review" if item.spec.get("review") else "schedule"
        return item.agent_id, item.id, source

    async def _timer_done(self, job_id: str) -> None:
        kind, _, ident = job_id.partition(":")
        if kind != "item":
            return
        item = await self._db.run(lambda c: store.get_item(c, ident))
        if item is not None and item.spec.get("type") == "timer":
            await self._db.run(lambda c: store.delete_item(c, ident))

    def _on_missed(self, event: JobExecutionEvent) -> None:
        """APScheduler missed a job while the app ran (a stalled loop)."""
        self._spawn(self._missed_at_runtime(event.job_id, event.scheduled_run_time))

    async def _missed_at_runtime(self, job_id: str, due: datetime) -> None:
        await self._skip_missed(job_id, due, 1)
        if self._scheduler is not None and self._scheduler.get_job(job_id) is None:
            await self._timer_done(job_id)

    async def fire(
        self, job_id: str, *, due: datetime | None = None, missed_count: int = 1
    ) -> None:
        """Run one job: submit its invocation, or record why not."""
        due = due or self._clock()
        kind, _, ident = job_id.partition(":")
        if kind == "review":
            await self._fire_review(ident, due, missed_count)
        elif kind == "item":
            await self._fire_item(ident, due, missed_count)

    async def _fire_review(self, agent_id: str, due: datetime, count: int) -> None:
        agent = await self._reader.get(agent_id)
        if agent is None or not agent.definition.review.enabled:
            self._drop_job(f"review:{agent_id}")
            return
        request = InvocationRequest(
            agent_id,
            "review",
            data={
                "review": {
                    "schedule": "definition",
                    "cron": agent.definition.review.cron,
                    "due_at": _iso(due),
                    "missed_count": count,
                }
            },
            caller={"review": "definition"},
        )
        await self._submit(request, None, "review", due, count)

    async def _fire_item(self, item_id: str, due: datetime, count: int) -> None:
        item = await self._db.run(lambda c: store.get_item(c, item_id))
        if item is None or not item.enabled or item.kind != "schedule":
            self._drop_job(f"item:{item_id}")
            return
        spec = item.spec
        source = "review" if spec.get("review") else "schedule"
        agent = await self._reader.get(item.agent_id)
        try:
            if agent is None:
                return
            if await self._too_soon(agent):
                await self._db.run(
                    lambda c: store.insert_run(
                        c,
                        agent_id=item.agent_id,
                        item_id=item.id,
                        source=source,
                        due_at=_iso(due),
                        at=_iso(self._clock()),
                        status="skipped_min_interval",
                        missed_count=count,
                    )
                )
                return
            schedule = {
                "id": item.id,
                "type": spec["type"],
                "due_at": _iso(due),
                "missed_count": count,
            }
            for key in ("cron", "note"):
                if key in spec:
                    schedule[key] = spec[key]
            data: dict[str, Any] = {"schedule": schedule}
            if "data" in spec:
                data["data"] = spec["data"]
            request = InvocationRequest(
                item.agent_id, source, data=data, caller={"schedule_id": item.id}
            )
            await self._submit(request, item.id, source, due, count)
        finally:
            if spec.get("type") == "timer":
                self._drop_job(f"item:{item.id}")
                await self._db.run(lambda c: store.delete_item(c, item.id))

    async def _too_soon(self, agent: Agent) -> bool:
        """True if the agent had a schedule run less than the minimum ago."""
        last = await self._db.run(lambda c: store.last_scheduled_run(c, agent.id))
        if last is None:
            return False
        minimum = agent.definition.scheduling.min_interval_seconds
        return (self._clock() - _parse(last)).total_seconds() < minimum

    async def _submit(
        self,
        request: InvocationRequest,
        item_id: str | None,
        source: str,
        due: datetime,
        count: int,
    ) -> None:
        at = _iso(self._clock())
        try:
            handle: Handle = await self._router.submit(request)
        except Exception as err:
            _LOG.warning("cannot submit %s invocation: %s", source, err)
            code = type(err).__name__
            await self._db.run(
                lambda c: store.insert_run(
                    c,
                    agent_id=request.agent_id,
                    item_id=item_id,
                    source=source,
                    due_at=_iso(due),
                    at=at,
                    status="skipped_unavailable",
                    missed_count=count,
                    result_code=code,
                )
            )
            return
        run_id = await self._db.run(
            lambda c: store.insert_run(
                c,
                agent_id=request.agent_id,
                item_id=item_id,
                source=source,
                due_at=_iso(due),
                at=at,
                status="submitted",
                missed_count=count,
                invocation_id=handle.invocation_id,
            )
        )
        self._spawn(self._await_result(handle, run_id))

    async def _await_result(self, handle: Handle, run_id: int) -> None:
        result = await handle.result()
        await self._db.run(
            lambda c: store.finish_run(
                c, run_id, result_status=result.status, result_code=result.error_code
            )
        )

    # ---------------------------------------------------- agent changes

    def _on_change(self, event: ChangeEvent) -> None:
        if event.topic == "agents":
            self._spawn(self._agent_changed(event.id, event.action))

    async def _agent_changed(self, agent_id: str, action: str) -> None:
        agent = None if action == "deleted" else await self._reader.get(agent_id)
        if agent is None:
            self._agents.pop(agent_id, None)
            self._drop_job(f"review:{agent_id}")
            ids = await self._db.run(lambda c: store.delete_agent_items(c, agent_id))
            for item_id in ids:
                self._drop_job(f"item:{item_id}")
                self._unindex(item_id)
            await self._sync_ha()
            return
        self._agents[agent_id] = agent
        if agent.definition.review.enabled:
            self._put_job(f"review:{agent_id}", *self._review_trigger(agent))
        else:
            self._drop_job(f"review:{agent_id}")

    # ------------------------------------------------------ the store

    async def get_item(self, item_id: str) -> store.Item | None:
        """One item, or ``None``."""
        return await self._db.run(lambda c: store.get_item(c, item_id))

    async def _owned(self, agent_id: str, kind: str, item_id: str) -> store.Item:
        item = await self.get_item(item_id)
        if item is None or item.agent_id != agent_id or item.kind != kind:
            raise StoreError("not_owner", f"the {kind} does not belong to the agent")
        return item

    def _public(self, item: store.Item) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": item.id,
            "item_id": item.id,
            "kind": item.kind,
            "description": _describe(item),
            "enabled": item.enabled,
            "spec": item.spec,
            "created_at": item.created_at,
            "updated_at": item.updated_at,
        }
        if item.kind == "schedule":
            job = (
                None
                if self._scheduler is None
                else self._scheduler.get_job(f"item:{item.id}")
            )
            next_run = None if job is None else job.next_run_time
            data["next_run_at"] = None if next_run is None else _iso(next_run)
        else:
            data["debounce_seconds"] = self._debounce(item.agent_id, item.spec)
        return data

    async def list_public(self, agent_id: str, kind: str) -> list[dict[str, Any]]:
        """The items of one kind that the agent owns, for ``self.list``."""
        items = await self._db.run(
            lambda c: store.list_items(c, agent_id=agent_id, kind=kind)  # type: ignore[arg-type]
        )
        return [self._public(item) for item in items]

    async def _agent(self, agent_id: str) -> Agent:
        agent = await self._reader.get(agent_id)
        if agent is None:
            raise StoreError("agent_not_found", "the agent does not exist")
        self._agents[agent_id] = agent
        return agent

    def _check_schedule(self, agent: Agent, spec: Mapping[str, Any]) -> str | None:
        """Check the time limits of a schedule. Return the timer due time."""
        now = self._clock()
        minimum = agent.definition.scheduling.min_interval_seconds
        if spec["type"] == "timer":
            if "delay_seconds" in spec:
                run_at = now + timedelta(seconds=int(spec["delay_seconds"]))
            else:
                run_at = _parse(str(spec["at"]))
            ahead = (run_at - now).total_seconds()
            if ahead < minimum:
                raise StoreError(
                    "min_interval",
                    f"a timer must be at least {minimum} seconds in the future",
                )
            if ahead > MAX_TIMER_DELAY:
                raise StoreError("too_far", "a timer must be at most 366 days ahead")
            return _iso(run_at)
        trigger = cron_trigger(str(spec["cron"]), self._tz)
        if trigger.get_next_fire_time(None, now) is None:
            raise StoreError("never_fires", "the cron expression never fires")
        gap = min_gap_seconds(trigger, now)
        if gap is not None and gap < minimum:
            raise StoreError(
                "min_interval",
                f"the schedule fires {gap:.0f} seconds apart; the minimum "
                f"interval is {minimum} seconds",
            )
        return None

    async def create_item(
        self, agent_id: str, kind: str, spec: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Create a schedule or a subscription, within the definition limits."""
        agent = await self._agent(agent_id)
        run_at = self._check_schedule(agent, spec) if kind == "schedule" else None
        limit = agent.definition.scheduling.max_schedules
        now = _iso(self._clock())
        try:
            item = await self._db.run(
                lambda c: store.insert_item(
                    c,
                    agent_id=agent_id,
                    kind=kind,  # type: ignore[arg-type]
                    spec=spec,
                    run_at=run_at,
                    limit=limit,
                    now=now,
                )
            )
        except store.LimitError:
            raise StoreError(
                "limit_reached",
                f"the agent can have at most {limit} schedules and subscriptions",
            ) from None
        await self._activate(item)
        return self._public(item)

    async def update_item(
        self, agent_id: str, kind: str, item_id: str, spec: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Replace the spec of an item that the agent owns."""
        await self._owned(agent_id, kind, item_id)
        agent = await self._agent(agent_id)
        run_at = self._check_schedule(agent, spec) if kind == "schedule" else None
        now = _iso(self._clock())
        item = await self._db.run(
            lambda c: store.update_item(c, item_id, spec=spec, run_at=run_at, now=now)
        )
        if item is None:
            raise StoreError("not_owner", f"the {kind} does not belong to the agent")
        await self._deactivate(item)
        await self._activate(item)
        return self._public(item)

    async def set_item_enabled(
        self, agent_id: str, kind: str, item_id: str, enabled: bool
    ) -> None:
        """Turn an item on or off."""
        current = await self._owned(agent_id, kind, item_id)
        if (
            enabled
            and current.run_at is not None
            and _parse(current.run_at) <= self._clock()
        ):
            raise StoreError("in_the_past", "the timer time has passed")
        now = _iso(self._clock())
        item = await self._db.run(lambda c: store.set_enabled(c, item_id, enabled, now))
        if item is None:
            return
        await self._deactivate(item)
        await self._activate(item)

    async def delete_item(self, agent_id: str, kind: str, item_id: str) -> None:
        """Delete an item."""
        item = await self._owned(agent_id, kind, item_id)
        await self._db.run(lambda c: store.delete_item(c, item_id))
        await self._deactivate(item)

    async def _activate(self, item: store.Item) -> None:
        if not item.enabled:
            return
        if item.kind == "schedule":
            self._put_job(f"item:{item.id}", *self._trigger(item))
        else:
            self._index(item)
            await self._sync_ha()

    async def _deactivate(self, item: store.Item) -> None:
        if item.kind == "schedule":
            self._drop_job(f"item:{item.id}")
        else:
            self._unindex(item.id)
            await self._sync_ha()

    # --------------------------------------------------- subscriptions

    def _debounce(self, agent_id: str, spec: Mapping[str, Any]) -> float:
        """The debounce of a subscription: the definition value is a floor."""
        agent = self._agents.get(agent_id)
        floor = 0.0 if agent is None else agent.definition.scheduling.debounce_seconds
        return max(float(spec.get("debounce_seconds") or 0.0), floor)

    def _index(self, item: store.Item) -> None:
        spec = item.spec
        event_type = str(spec.get("event_type", "state_changed"))
        if event_type in DENIED_EVENT_TYPES:
            return  # Defence in depth: the tools refuse these specs.
        self._unindex(item.id)
        sub = _Sub(
            item_id=item.id,
            agent_id=item.agent_id,
            spec=dict(spec),
            event_type=event_type,
            entity_ids=frozenset(spec.get("entity_ids") or ()),
            include_own_writes=bool(spec.get("include_own_writes", False)),
            debounce=float(spec.get("debounce_seconds") or 0.0),
        )
        self._subs[item.id] = sub
        for entity_id in sub.entity_ids:
            self._by_entity.setdefault(entity_id, set()).add(item.id)

    def _unindex(self, item_id: str) -> None:
        sub = self._subs.pop(item_id, None)
        if sub is None:
            return
        if sub.timer is not None:
            sub.timer.cancel()
        for entity_id in sub.entity_ids:
            ids = self._by_entity.get(entity_id)
            if ids is not None:
                ids.discard(item_id)
                if not ids:
                    del self._by_entity[entity_id]

    async def _sync_ha(self) -> None:
        """Hold one HA event subscription for each event type in use."""
        if self._ctx is None:
            return
        ha = self._ctx.ha
        async with self._ha_lock:
            wanted = {sub.event_type for sub in self._subs.values()}
            wanted -= DENIED_EVENT_TYPES
            for event_type in list(self._ha_subs):
                if event_type not in wanted:
                    await ha.unsubscribe(self._ha_subs.pop(event_type))
            for event_type in sorted(wanted - set(self._ha_subs)):
                self._ha_subs[event_type] = await ha.subscribe_events(
                    self._on_event, event_type
                )

    def _on_event(self, event: Mapping[str, Any]) -> None:
        """The HA event handler. It must be fast, so it only queues."""
        self._queue.put_nowait(event)

    async def _consume(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                self._accept(event)
            except Exception:
                _LOG.exception("cannot handle an HA event")

    async def _tick(self) -> None:
        last_prune = self._monotonic()
        while True:
            await asyncio.sleep(TICK_SECONDS)
            if self.tracker is not None:
                self.handle_classified(self.tracker.tick())
            if self._monotonic() - last_prune > PRUNE_SECONDS:
                last_prune = self._monotonic()
                await self._prune()

    async def _prune(self) -> None:
        before = self._clock() - timedelta(days=self._config.retention_days)
        await self._db.run(lambda c: store.prune(c, before))

    def _matching(self, event: Mapping[str, Any]) -> list[_Sub]:
        event_type = event.get("event_type")
        if event_type == "state_changed":
            entity_id = (event.get("data") or {}).get("entity_id")
            ids = self._by_entity.get(str(entity_id), set())
            return [self._subs[i] for i in sorted(ids) if i in self._subs]
        return [s for s in self._subs.values() if s.event_type == event_type]

    def _accept(self, event: Mapping[str, Any]) -> None:
        if self.tracker is None or not self._matching(event):
            return
        context_id = str((event.get("context") or {}).get("id") or "")
        received = _iso(self._clock())
        self.handle_classified(self.tracker.event(context_id, (event, received)))

    def handle_classified(self, released: list[Classified[Payload]]) -> None:
        """Route classified events to their subscriptions."""
        ignored: list[store.EventRow] = []
        loop = asyncio.get_running_loop()
        for classified in released:
            event, _ = classified.payload
            for sub in self._matching(event):
                own = (
                    classified.origin == "agent" and classified.agent_id == sub.agent_id
                )
                if own and not sub.include_own_writes:
                    ignored.append(self._row(sub, classified, "own_write"))
                    continue
                sub.pending.append(classified)
                if sub.timer is None:
                    delay = self._debounce(sub.agent_id, sub.spec)
                    sub.timer = loop.call_later(delay, self._flush, sub.item_id)
        if ignored:
            self._spawn(self._db.run(lambda c: store.insert_events(c, ignored)))

    def _flush(self, item_id: str) -> None:
        sub = self._subs.get(item_id)
        if sub is None:
            return
        changes, sub.pending, sub.timer = sub.pending, [], None
        if changes:
            self._spawn(self._deliver(sub, changes))

    @staticmethod
    def _row(
        sub: _Sub,
        classified: Classified[Payload],
        outcome: str,
        *,
        detail: str | None = None,
        invocation_id: str | None = None,
    ) -> store.EventRow:
        event, received = classified.payload
        data = event.get("data") or {}
        entity_id = data.get("entity_id") if sub.event_type == "state_changed" else None
        return store.EventRow(
            subscription_id=sub.item_id,
            agent_id=sub.agent_id,
            event_type=sub.event_type,
            entity_id=None if entity_id is None else str(entity_id),
            context_id=classified.context_id or None,
            origin=classified.origin,
            origin_agent_id=classified.agent_id,
            received_at=received,
            outcome=outcome,
            detail=detail,
            invocation_id=invocation_id,
        )

    def _tool_env(self) -> ToolEnv | None:
        registry = None if self._tools is None else self._tools.registry
        if registry is None or self._ctx is None:
            return None
        return ToolEnv(ha=self._ctx.ha, states=self._ctx.states, registry=registry)

    def _recheck(self, agent: Agent, sub: _Sub) -> str | None:
        """Check the subscription against the current scope. Return a denial."""
        env = self._tool_env()
        if env is None:
            return "registry_unavailable"
        try:
            SPEC_CHECKS["subscription"](ToolContext(agent), env, sub.spec)
        except ScopeDeniedError as err:
            return err.code
        return None

    async def _deliver(self, sub: _Sub, changes: list[Classified[Payload]]) -> None:
        agent = await self._reader.get(sub.agent_id)

        async def record(
            outcome: str, detail: str | None = None, invocation_id: str | None = None
        ) -> None:
            rows = [
                self._row(sub, c, outcome, detail=detail, invocation_id=invocation_id)
                for c in changes
            ]
            await self._db.run(lambda c: store.insert_events(c, rows))

        if agent is None or not agent.enabled:
            await record("dropped", "agent_unavailable")
            return
        denial = self._recheck(agent, sub)
        if denial is not None:
            await record("denied", denial)
            return
        data = redact(self._payload(sub, changes))
        request = InvocationRequest(
            agent.id, "event", data=data, caller={"subscription_id": sub.item_id}
        )
        try:
            handle: Handle = await self._router.submit(request)
        except Exception as err:
            await record("dropped", type(err).__name__)
            return
        await record("delivered", invocation_id=handle.invocation_id)

    def _payload(self, sub: _Sub, changes: list[Classified[Payload]]) -> dict[str, Any]:
        shown = changes[-self._config.max_changes_per_delivery :]
        items: list[dict[str, Any]] = []
        for classified in shown:
            event, _ = classified.payload
            data = event.get("data") or {}
            entry: dict[str, Any] = {
                "origin": classified.origin,
                "own": classified.agent_id == sub.agent_id,
                "time": event.get("time_fired"),
            }
            if sub.event_type == "state_changed":
                entry["entity_id"] = data.get("entity_id")
                entry["old_state"] = _slim(data.get("old_state"))
                entry["new_state"] = _slim(data.get("new_state"))
            else:
                entry["data"] = data
            items.append(entry)
        return {
            "subscription": {"id": sub.item_id, "event_type": sub.event_type},
            "count": len(changes),
            "changes" if sub.event_type == "state_changed" else "events": items,
        }


def _describe(item: store.Item) -> str:
    """A short text for the item. It holds no value that changes by itself.

    The prompt builder puts it into the stable part of the prompt.
    """
    spec = item.spec
    if item.kind == "subscription":
        event_type = spec.get("event_type", "state_changed")
        entities = ", ".join(spec.get("entity_ids") or ())
        text = f"{event_type} {entities}".strip()
    elif spec.get("type") == "timer":
        text = f"timer at {item.run_at}"
    else:
        text = f"cron {spec.get('cron')}"
    if spec.get("review"):
        text += " (review)"
    if spec.get("note"):
        text += f": {spec['note']}"
    return text


def _slim(state: Any) -> dict[str, Any] | None:
    if not isinstance(state, Mapping):
        return None
    return {
        "state": state.get("state"),
        "attributes": state.get("attributes") or {},
        "last_changed": state.get("last_changed"),
    }


__all__ = [
    "JOB_FUNC",
    "SchedulerComponent",
    "SchedulerStateStore",
    "fire_job",
]
