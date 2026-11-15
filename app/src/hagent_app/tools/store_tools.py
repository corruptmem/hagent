"""The ``knowledge``, ``memory`` and ``self`` tool groups (design section 8).

Scope rules:

- ``knowledge``: the scope is the set of attached stores, with the access of
  each attachment. A read needs an attachment. A write or a delete needs
  ``read_write`` access. A search passes the attached store IDs only.
- ``memory``: the scope is the agent's own history. The agent ID comes from
  the invocation, never from the arguments. Rule statistics need a rule
  that the agent owns.
- ``self``: the scope is the agent's own rules, schedules and
  subscriptions. A change needs an item that the agent owns. Each spec kind
  has a strict model: unknown keys fail. A state subscription must name
  entities in the read scope. Other event types need the ``admin`` level,
  because events carry data about entities outside the scope. No level can
  subscribe to ``call_service``: its data holds the action IDs of approval
  notifications. Schedule specs are a timer or a cron schedule; the
  scheduler store also checks the definition limits (task-15). Rule specs
  have a strict model and scope checks (``rules/checks.py``, task-13).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    ValidationError,
    model_validator,
)

from hagent_app.agents.definition import Cron, KnowledgeAttachment
from hagent_app.tools.base import (
    Args,
    Prepared,
    Tool,
    ToolContext,
    ToolEnv,
    ToolUnavailableError,
)
from hagent_app.tools.ha_tools import EntityIds, scope_of
from hagent_app.tools.scope import ScopeDeniedError, check_read
from hagent_app.tools.stores import (
    AgentStateStore,
    HistoryStore,
    KnowledgeStore,
    StateKind,
)

StoreIdArg = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{32}$")]
_PATH_CHARS = re.compile(r"[A-Za-z0-9_\-./ ]{1,255}")


def _safe_path(value: str) -> str:
    """Reject a path that can leave its store: ``.``, ``..`` or empty parts."""
    if not _PATH_CHARS.fullmatch(value):
        msg = "a path uses letters, digits, '_', '-', '.', ' ' and '/' only"
        raise ValueError(msg)
    parts = value.split("/")
    if any(part.strip() in {"", ".", ".."} for part in parts):
        msg = "a path must not have empty, '.' or '..' parts"
        raise ValueError(msg)
    return value


PathArg = Annotated[str, AfterValidator(_safe_path)]
ItemId = Annotated[str, StringConstraints(min_length=1, max_length=128)]
Text = Annotated[str, StringConstraints(max_length=200_000)]
Query = Annotated[str, StringConstraints(min_length=1, max_length=1000)]


def _knowledge(env: ToolEnv) -> KnowledgeStore:
    if env.knowledge is None:
        msg = "knowledge stores are not available yet"
        raise ToolUnavailableError(msg)
    return env.knowledge


def _history(env: ToolEnv) -> HistoryStore:
    if env.history is None:
        msg = "history is not available yet"
        raise ToolUnavailableError(msg)
    return env.history


def _agent_state(env: ToolEnv) -> AgentStateStore:
    if env.agent_state is None:
        msg = "rules, schedules and subscriptions are not available yet"
        raise ToolUnavailableError(msg)
    return env.agent_state


# --------------------------------------------------------------- knowledge


def _attachment(
    ctx: ToolContext, store_id: str, *, write: bool = False
) -> KnowledgeAttachment:
    for attachment in ctx.definition.knowledge:
        if attachment.store_id == store_id:
            if write and attachment.access != "read_write":
                raise ScopeDeniedError(
                    "read_only", "the agent has read access only to this store"
                )
            return attachment
    raise ScopeDeniedError("store_not_attached", "the store is not attached")


class KnowledgeListArgs(Args):
    """Arguments of ``knowledge.list``."""

    store_id: StoreIdArg | None = None


class KnowledgeList(Tool):
    """List the attached stores, or the content of one attached store."""

    name = "knowledge.list"
    group = "knowledge"
    description = (
        "Without store_id: list your attached knowledge stores. With "
        "store_id: list the files and documents in that store."
    )
    args_model = KnowledgeListArgs

    async def authorize(
        self, ctx: ToolContext, env: ToolEnv, args: KnowledgeListArgs
    ) -> Prepared:
        if args.store_id is not None:
            _attachment(ctx, args.store_id)
            return Prepared(args.model_dump(), ("items", args.store_id))
        ids = tuple(a.store_id for a in ctx.definition.knowledge)
        return Prepared(args.model_dump(), ("stores", ids))

    async def run(self, ctx: ToolContext, env: ToolEnv, prepared: Prepared) -> Any:
        kind, value = prepared.payload
        store = _knowledge(env)
        if kind == "items":
            return await store.list_items(value)
        access = {a.store_id: a.access for a in ctx.definition.knowledge}
        stores = await store.describe(value)
        return [
            {**s, "access": access.get(str(s.get("id")))}
            for s in stores
            if s.get("id") in access
        ]


class KnowledgeReadArgs(Args):
    """Arguments of ``knowledge.read``."""

    store_id: StoreIdArg
    path: PathArg
    section: Annotated[str, StringConstraints(max_length=255)] | None = None


class KnowledgeRead(Tool):
    """Read a file or a document in an attached store."""

    name = "knowledge.read"
    group = "knowledge"
    description = "Read a file or document, or one section of it."
    args_model = KnowledgeReadArgs

    async def authorize(
        self, ctx: ToolContext, env: ToolEnv, args: KnowledgeReadArgs
    ) -> Prepared:
        _attachment(ctx, args.store_id)
        return Prepared(args.model_dump(), args)

    async def run(self, ctx: ToolContext, env: ToolEnv, prepared: Prepared) -> Any:
        args: KnowledgeReadArgs = prepared.payload
        return await _knowledge(env).read(args.store_id, args.path, args.section)


class KnowledgeSearchArgs(Args):
    """Arguments of ``knowledge.search``."""

    query: Query
    store_ids: Annotated[list[StoreIdArg], Field(max_length=100)] | None = None
    limit: Annotated[int, Field(ge=1, le=50)] = 10


class KnowledgeSearch(Tool):
    """Search the attached stores."""

    name = "knowledge.search"
    group = "knowledge"
    description = "Search all attached stores, or the stores that you name."
    args_model = KnowledgeSearchArgs

    async def authorize(
        self, ctx: ToolContext, env: ToolEnv, args: KnowledgeSearchArgs
    ) -> Prepared:
        attached = [a.store_id for a in ctx.definition.knowledge]
        if args.store_ids is None:
            ids = tuple(attached)
        else:
            for store_id in args.store_ids:
                _attachment(ctx, store_id)
            ids = tuple(dict.fromkeys(args.store_ids))
        if not ids:
            raise ScopeDeniedError("store_not_attached", "no store is attached")
        return Prepared(args.model_dump(), (ids, args.query, args.limit))

    async def run(self, ctx: ToolContext, env: ToolEnv, prepared: Prepared) -> Any:
        ids, query, limit = prepared.payload
        return await _knowledge(env).search(ids, query, limit, agent_id=ctx.agent.id)


class KnowledgeWriteArgs(Args):
    """Arguments of ``knowledge.write``."""

    store_id: StoreIdArg
    path: PathArg
    mode: Literal["create", "append", "replace"]
    content: Text
    old_text: Text | None = None
    base_version: Annotated[int, Field(ge=0)] | None = None


class KnowledgeWrite(Tool):
    """Create, append to or change a file in a ``read_write`` store."""

    name = "knowledge.write"
    group = "knowledge"
    description = (
        "Create a file, append to a file, or replace one exact passage "
        "(old_text). Give base_version, the version that you read."
    )
    args_model = KnowledgeWriteArgs
    writes = True

    async def authorize(
        self, ctx: ToolContext, env: ToolEnv, args: KnowledgeWriteArgs
    ) -> Prepared:
        _attachment(ctx, args.store_id, write=True)
        if args.mode == "replace" and not args.old_text:
            raise ScopeDeniedError("invalid_arguments", "replace needs old_text")
        request = args.model_dump(exclude={"content", "old_text"})
        request["content_length"] = len(args.content)
        return Prepared(request, args)

    async def run(self, ctx: ToolContext, env: ToolEnv, prepared: Prepared) -> Any:
        args: KnowledgeWriteArgs = prepared.payload
        return await _knowledge(env).write(
            args.store_id,
            args.path,
            args.mode,
            args.content,
            old_text=args.old_text,
            base_version=args.base_version,
            actor_agent_id=ctx.agent.id,
        )


class KnowledgeDeleteArgs(Args):
    """Arguments of ``knowledge.delete``."""

    store_id: StoreIdArg
    path: PathArg
    base_version: Annotated[int, Field(ge=0)] | None = None


class KnowledgeDelete(Tool):
    """Delete a file in a ``read_write`` store."""

    name = "knowledge.delete"
    group = "knowledge"
    description = "Delete a file. Documents cannot be deleted."
    args_model = KnowledgeDeleteArgs
    writes = True

    async def authorize(
        self, ctx: ToolContext, env: ToolEnv, args: KnowledgeDeleteArgs
    ) -> Prepared:
        _attachment(ctx, args.store_id, write=True)
        return Prepared(args.model_dump(), args)

    async def run(self, ctx: ToolContext, env: ToolEnv, prepared: Prepared) -> Any:
        args: KnowledgeDeleteArgs = prepared.payload
        await _knowledge(env).delete_file(
            args.store_id,
            args.path,
            base_version=args.base_version,
            actor_agent_id=ctx.agent.id,
        )
        return {"deleted": args.path}


# ------------------------------------------------------------------ memory


class MemorySearchArgs(Args):
    """Arguments of ``memory.search``."""

    query: Query
    source: Annotated[str, StringConstraints(max_length=200)] | None = None
    since: Annotated[str, StringConstraints(max_length=40)] | None = None
    until: Annotated[str, StringConstraints(max_length=40)] | None = None
    limit: Annotated[int, Field(ge=1, le=50)] = 10


class MemorySearch(Tool):
    """Search the agent's own history and conversations."""

    name = "memory.search"
    group = "memory"
    description = "Search your own history and conversations."
    args_model = MemorySearchArgs

    async def authorize(
        self, ctx: ToolContext, env: ToolEnv, args: MemorySearchArgs
    ) -> Prepared:
        return Prepared(args.model_dump(), args)

    async def run(self, ctx: ToolContext, env: ToolEnv, prepared: Prepared) -> Any:
        args: MemorySearchArgs = prepared.payload
        filters = args.model_dump(exclude={"query", "limit"}, exclude_none=True)
        return await _history(env).search(ctx.agent.id, args.query, filters, args.limit)


class RuleStatsArgs(Args):
    """Arguments of ``memory.rule_stats``."""

    rule_id: ItemId


class RuleStats(Tool):
    """Read the statistics of one of the agent's rules."""

    name = "memory.rule_stats"
    group = "memory"
    description = "Read the statistics of one of your rules."
    args_model = RuleStatsArgs

    async def authorize(
        self, ctx: ToolContext, env: ToolEnv, args: RuleStatsArgs
    ) -> Prepared:
        owner = await _history(env).rule_owner(args.rule_id)
        if owner != ctx.agent.id:
            raise ScopeDeniedError("not_owner", "the rule does not belong to the agent")
        return Prepared(args.model_dump(), args.rule_id)

    async def run(self, ctx: ToolContext, env: ToolEnv, prepared: Prepared) -> Any:
        return await _history(env).rule_stats(ctx.agent.id, prepared.payload)


# -------------------------------------------------------------------- self

Kind = Literal["rule", "schedule", "subscription"]
Spec = dict[str, JsonValue]


class _SpecModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


Debounce = Annotated[float, Field(ge=0, le=86400)]


class StateSubscriptionSpec(_SpecModel):
    """A subscription to state changes of entities in the read scope."""

    event_type: Literal["state_changed"] = "state_changed"
    entity_ids: EntityIds
    debounce_seconds: Debounce | None = None
    include_own_writes: bool = False


# Event types that no agent can subscribe to, at any level. A call_service
# event carries the full service data of every call, also the action IDs of
# the app's approval notifications. An agent that saw them could send its
# own notification with a real accept action (task-16 review, M3).
DENIED_EVENT_TYPES = frozenset({"call_service"})


class EventSubscriptionSpec(_SpecModel):
    """A subscription to another HA event type (``admin`` only)."""

    event_type: Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,64}$")]
    debounce_seconds: Debounce | None = None


type SpecCheck = Callable[[ToolContext, ToolEnv, Spec], dict[str, Any]]


def _subscription_spec(ctx: ToolContext, env: ToolEnv, spec: Spec) -> dict[str, Any]:
    event_type = spec.get("event_type", "state_changed")
    model: type[_SpecModel]
    if event_type in DENIED_EVENT_TYPES:
        raise ScopeDeniedError(
            "event_not_permitted",
            f"no agent can subscribe to {event_type} events",
        )
    if event_type == "state_changed":
        model = StateSubscriptionSpec
    elif ctx.definition.tools.ha.level != "admin":
        raise ScopeDeniedError(
            "event_not_permitted",
            "only state subscriptions are permitted below the admin level",
        )
    else:
        model = EventSubscriptionSpec
    try:
        checked = model.model_validate(spec)
    except ValidationError as err:
        raise ScopeDeniedError("invalid_spec", _spec_message(err)) from err
    if isinstance(checked, StateSubscriptionSpec):
        check_read(scope_of(ctx, env), checked.entity_ids)
    return checked.model_dump(exclude_none=True)


# ------------------------------------------------------------- schedules

MAX_TIMER_DELAY = 366 * 86_400
MAX_SCHEDULE_DATA_CHARS = 4000
Note = Annotated[str, StringConstraints(max_length=500)]


def _aware_iso(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as err:
        msg = "at must be an ISO 8601 date and time"
        raise ValueError(msg) from err
    if parsed.tzinfo is None:
        msg = "at must include a UTC offset, for example +00:00"
        raise ValueError(msg)
    return value


def _cron_days(value: str) -> str:
    fields = value.split()
    if fields[2] != "*" and fields[4] != "*":
        msg = "set the day of the month or the day of the week, not both"
        raise ValueError(msg)
    return value


AwareTime = Annotated[str, StringConstraints(max_length=40), AfterValidator(_aware_iso)]
ScheduleCron = Annotated[Cron, AfterValidator(_cron_days)]


class _ScheduleSpec(_SpecModel):
    review: bool = False
    note: Note | None = None
    data: dict[str, JsonValue] | None = None

    @model_validator(mode="after")
    def _check_data(self) -> _ScheduleSpec:
        if self.data is not None:
            size = len(json.dumps(self.data, ensure_ascii=False))
            if size > MAX_SCHEDULE_DATA_CHARS:
                msg = f"data must be at most {MAX_SCHEDULE_DATA_CHARS} characters"
                raise ValueError(msg)
        return self


class TimerScheduleSpec(_ScheduleSpec):
    """A timer: one invocation after ``delay_seconds``, or at ``at``.

    ``review`` makes the invocation an extra review (design section 14).
    ``note`` and ``data`` go to the invocation as caller data.
    """

    type: Literal["timer"]
    delay_seconds: Annotated[int, Field(ge=1, le=MAX_TIMER_DELAY)] | None = None
    at: AwareTime | None = None

    @model_validator(mode="after")
    def _check_time(self) -> TimerScheduleSpec:
        if (self.delay_seconds is None) == (self.at is None):
            msg = "set exactly one of delay_seconds and at"
            raise ValueError(msg)
        return self


class CronScheduleSpec(_ScheduleSpec):
    """A cron schedule: a 5-field cron expression in the local time zone."""

    type: Literal["cron"]
    cron: ScheduleCron


def _schedule_spec(ctx: ToolContext, env: ToolEnv, spec: Spec) -> dict[str, Any]:
    """Check a schedule spec. The store checks the time limits again."""
    kind = spec.get("type")
    model: type[_ScheduleSpec]
    if kind == "timer":
        model = TimerScheduleSpec
    elif kind == "cron":
        model = CronScheduleSpec
    else:
        raise ScopeDeniedError("invalid_spec", 'spec.type: must be "timer" or "cron"')
    try:
        checked = model.model_validate(spec)
    except ValidationError as err:
        raise ScopeDeniedError("invalid_spec", _spec_message(err)) from err
    minimum = ctx.definition.scheduling.min_interval_seconds
    if isinstance(checked, TimerScheduleSpec) and (
        checked.delay_seconds is not None and checked.delay_seconds < minimum
    ):
        raise ScopeDeniedError(
            "min_interval",
            f"a timer must be at least {minimum} seconds in the future",
        )
    return checked.model_dump(exclude_none=True)


def _spec_message(err: ValidationError) -> str:
    parts = []
    for item in err.errors(include_url=False, include_context=False)[:5]:
        loc = ".".join(str(p) for p in item["loc"]) or "spec"
        parts.append(f"spec.{loc}: {item['msg']}")
    return "; ".join(parts)


def _rule_spec(ctx: ToolContext, env: ToolEnv, spec: Spec) -> dict[str, Any]:
    # Imported here: the rules package imports the tools (import cycle).
    from hagent_app.rules.checks import check_rule_spec  # noqa: PLC0415

    return check_rule_spec(ctx, env, spec)


# The seam for the spec kinds. A kind without an entry is not supported yet.
# Rules (task-13): rules/checks.py, checked again before each rule run.
# The scheduler runs the subscription check again before each delivery.
SPEC_CHECKS: dict[StateKind, SpecCheck] = {
    "subscription": _subscription_spec,
    "schedule": _schedule_spec,
    "rule": _rule_spec,
}


def _require_supported(kind: StateKind) -> SpecCheck:
    check = SPEC_CHECKS.get(kind)
    if check is None:
        raise ScopeDeniedError(
            "not_supported",
            f"{kind} specs are not supported yet; the runtime cannot check them",
        )
    return check


async def _check_owner(
    ctx: ToolContext, env: ToolEnv, kind: StateKind, item_id: str
) -> None:
    owner = await _agent_state(env).owner(kind, item_id)
    if owner != ctx.agent.id:
        raise ScopeDeniedError("not_owner", f"the {kind} does not belong to the agent")


class SelfListArgs(Args):
    """Arguments of ``self.list``."""

    kind: Kind


class SelfList(Tool):
    """List the agent's rules, schedules or subscriptions."""

    name = "self.list"
    group = "self"
    description = "List your rules, schedules or subscriptions."
    args_model = SelfListArgs

    async def authorize(
        self, ctx: ToolContext, env: ToolEnv, args: SelfListArgs
    ) -> Prepared:
        return Prepared(args.model_dump(), args.kind)

    async def run(self, ctx: ToolContext, env: ToolEnv, prepared: Prepared) -> Any:
        return await _agent_state(env).list_items(ctx.agent.id, prepared.payload)


class SelfCreateArgs(Args):
    """Arguments of ``self.create``."""

    kind: Kind
    spec: Spec


class SelfCreate(Tool):
    """Create a rule, a schedule or a subscription for the agent."""

    name = "self.create"
    group = "self"
    description = "Create a rule, a schedule or a subscription."
    args_model = SelfCreateArgs
    writes = True

    async def authorize(
        self, ctx: ToolContext, env: ToolEnv, args: SelfCreateArgs
    ) -> Prepared:
        spec = _require_supported(args.kind)(ctx, env, args.spec)
        checked = args.model_copy(update={"spec": spec})
        return Prepared(checked.model_dump(), checked)

    async def run(self, ctx: ToolContext, env: ToolEnv, prepared: Prepared) -> Any:
        args: SelfCreateArgs = prepared.payload
        return await _agent_state(env).create(ctx.agent.id, args.kind, args.spec)


class SelfUpdateArgs(Args):
    """Arguments of ``self.update``."""

    kind: Kind
    item_id: ItemId
    spec: Spec


class SelfUpdate(Tool):
    """Change one of the agent's rules, schedules or subscriptions."""

    name = "self.update"
    group = "self"
    description = "Change one of your rules, schedules or subscriptions."
    args_model = SelfUpdateArgs
    writes = True

    async def authorize(
        self, ctx: ToolContext, env: ToolEnv, args: SelfUpdateArgs
    ) -> Prepared:
        check = _require_supported(args.kind)
        await _check_owner(ctx, env, args.kind, args.item_id)
        spec = check(ctx, env, args.spec)
        checked = args.model_copy(update={"spec": spec})
        return Prepared(checked.model_dump(), checked)

    async def run(self, ctx: ToolContext, env: ToolEnv, prepared: Prepared) -> Any:
        args: SelfUpdateArgs = prepared.payload
        return await _agent_state(env).update(
            ctx.agent.id, args.kind, args.item_id, args.spec
        )


class SelfEnableArgs(Args):
    """Arguments of ``self.set_enabled``."""

    kind: Kind
    item_id: ItemId
    enabled: bool


class SelfEnable(Tool):
    """Turn one of the agent's items on or off."""

    name = "self.set_enabled"
    group = "self"
    description = "Turn one of your rules, schedules or subscriptions on or off."
    args_model = SelfEnableArgs
    writes = True

    async def authorize(
        self, ctx: ToolContext, env: ToolEnv, args: SelfEnableArgs
    ) -> Prepared:
        await _check_owner(ctx, env, args.kind, args.item_id)
        return Prepared(args.model_dump(), args)

    async def run(self, ctx: ToolContext, env: ToolEnv, prepared: Prepared) -> Any:
        args: SelfEnableArgs = prepared.payload
        await _agent_state(env).set_enabled(
            ctx.agent.id, args.kind, args.item_id, args.enabled
        )
        return {"item_id": args.item_id, "enabled": args.enabled}


class SelfDeleteArgs(Args):
    """Arguments of ``self.delete``."""

    kind: Kind
    item_id: ItemId


class SelfDelete(Tool):
    """Delete one of the agent's items."""

    name = "self.delete"
    group = "self"
    description = "Delete one of your rules, schedules or subscriptions."
    args_model = SelfDeleteArgs
    writes = True

    async def authorize(
        self, ctx: ToolContext, env: ToolEnv, args: SelfDeleteArgs
    ) -> Prepared:
        await _check_owner(ctx, env, args.kind, args.item_id)
        return Prepared(args.model_dump(), args)

    async def run(self, ctx: ToolContext, env: ToolEnv, prepared: Prepared) -> Any:
        args: SelfDeleteArgs = prepared.payload
        await _agent_state(env).delete(ctx.agent.id, args.kind, args.item_id)
        return {"deleted": args.item_id}


class ReorderRulesArgs(Args):
    """Arguments of ``self.reorder_rules``."""

    rule_ids: Annotated[list[ItemId], Field(min_length=1, max_length=500)]


class SelfReorderRules(Tool):
    """Set the order in which the router tests the agent's rules."""

    name = "self.reorder_rules"
    group = "self"
    description = (
        "Set the order of your rules. Name each of your rules exactly once. "
        "The router tests rules in this order."
    )
    args_model = ReorderRulesArgs
    writes = True

    async def authorize(
        self, ctx: ToolContext, env: ToolEnv, args: ReorderRulesArgs
    ) -> Prepared:
        for rule_id in dict.fromkeys(args.rule_ids):
            await _check_owner(ctx, env, "rule", rule_id)
        return Prepared(args.model_dump(), tuple(args.rule_ids))

    async def run(self, ctx: ToolContext, env: ToolEnv, prepared: Prepared) -> Any:
        order = await _agent_state(env).reorder_rules(ctx.agent.id, prepared.payload)
        return {"rule_ids": order}


class RollbackRuleArgs(Args):
    """Arguments of ``self.rollback_rule``."""

    rule_id: ItemId
    version: Annotated[int, Field(ge=1)]


class SelfRollbackRule(Tool):
    """Make an older version of one of the agent's rules the current one."""

    name = "self.rollback_rule"
    group = "self"
    description = (
        "Roll one of your rules back to an older version. The state of the "
        "rule does not change. Read the versions with memory.rule_stats."
    )
    args_model = RollbackRuleArgs
    writes = True

    async def authorize(
        self, ctx: ToolContext, env: ToolEnv, args: RollbackRuleArgs
    ) -> Prepared:
        await _check_owner(ctx, env, "rule", args.rule_id)
        return Prepared(args.model_dump(), args)

    async def run(self, ctx: ToolContext, env: ToolEnv, prepared: Prepared) -> Any:
        args: RollbackRuleArgs = prepared.payload
        return await _agent_state(env).rollback_rule(
            ctx.agent.id, args.rule_id, args.version
        )


class BacktestRuleArgs(Args):
    """Arguments of ``self.backtest_rule``."""

    rule_id: ItemId
    version: Annotated[int, Field(ge=1)] | None = None
    days: Annotated[int, Field(ge=1, le=30)] | None = None
    limit: Annotated[int, Field(ge=1, le=1000)] | None = None
    live: bool = True


class SelfBacktestRule(Tool):
    """Test one version of one of the agent's rules against its history.

    The backtest writes nothing (design section 10.7). It replays the past
    invocations that an agent turn answered, and compares the rule result
    with the turn result through the agreement template of the rule. A rule
    with a decision action can cost one decision request for each matched
    invocation: ``limit`` sets the most matched invocations. The tool is
    marked as a write, because it costs money and changes the statistics:
    a test invocation records it but does not run it.
    """

    name = "self.backtest_rule"
    group = "self"
    description = (
        "Backtest one of your rules: run a rule version (default: the "
        "current one) against your past invocations of the last 'days' "
        "days (default 7), without writes, and compare each rule result "
        "with your past result through the rule's agreement template. "
        "Returns the agreement rate, the cost and the cases. A decision "
        "rule can cost one decision request per matched invocation; set "
        "'limit' to cap them, or live=false to replay recorded answers only."
    )
    args_model = BacktestRuleArgs
    writes = True

    async def authorize(
        self, ctx: ToolContext, env: ToolEnv, args: BacktestRuleArgs
    ) -> Prepared:
        await _check_owner(ctx, env, "rule", args.rule_id)
        return Prepared(args.model_dump(), args)

    async def run(self, ctx: ToolContext, env: ToolEnv, prepared: Prepared) -> Any:
        args: BacktestRuleArgs = prepared.payload
        result = await _agent_state(env).backtest_rule(
            ctx.agent,
            args.rule_id,
            args.model_dump(exclude={"rule_id"}, exclude_none=True),
        )
        return _backtest_summary(result)


def _backtest_summary(result: Mapping[str, Any]) -> dict[str, Any]:
    """The backtest for the model: the totals and at most 20 notable cases."""
    summary = {k: v for k, v in result.items() if k != "cases"}
    cases = [
        c
        for c in result.get("cases") or []
        if c.get("outcome") in {"disagreed", "error", "skipped"}
    ]
    summary["notable_cases"] = cases[:20]
    return summary


STORE_TOOLS: tuple[Tool, ...] = (
    KnowledgeList(),
    KnowledgeRead(),
    KnowledgeSearch(),
    KnowledgeWrite(),
    KnowledgeDelete(),
    MemorySearch(),
    RuleStats(),
    SelfList(),
    SelfCreate(),
    SelfUpdate(),
    SelfEnable(),
    SelfDelete(),
    SelfReorderRules(),
    SelfRollbackRule(),
    SelfBacktestRule(),
)
