"""The ``knowledge``, ``memory`` and ``self`` tool groups (design section 8).

Scope rules:

- ``knowledge``: the scope is the set of attached stores, with the access of
  each attachment. A read needs an attachment. A write or a delete needs
  ``read_write`` access. A search passes the attached store IDs only.
- ``memory``: the scope is the agent's own history. The agent ID comes from
  the invocation, never from the arguments. Rule statistics need a rule
  that the agent owns.
- ``self``: the scope is the agent's own rules, schedules and
  subscriptions. A change needs an item that the agent owns. A state
  subscription must name entities in the read scope. Other event types
  need the ``admin`` level, because events such as ``call_service`` carry
  data about entities outside the scope.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field, JsonValue, StringConstraints

from hagent_app.agents.definition import KnowledgeAttachment
from hagent_app.tools.base import (
    Args,
    Prepared,
    Tool,
    ToolContext,
    ToolEnv,
    ToolUnavailableError,
)
from hagent_app.tools.ha_tools import scope_of
from hagent_app.tools.scope import ScopeDeniedError, check_read
from hagent_app.tools.stores import (
    AgentStateStore,
    HistoryStore,
    KnowledgeStore,
    StateKind,
)

StoreIdArg = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{32}$")]
PathArg = Annotated[
    str,
    StringConstraints(
        min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_\-][A-Za-z0-9_\-./ ]*$"
    ),
]
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
        return await _knowledge(env).search(ids, query, limit)


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


def _check_spec(ctx: ToolContext, env: ToolEnv, kind: StateKind, spec: Spec) -> None:
    """Check the HA references of a subscription against the read scope."""
    if kind != "subscription":
        return
    event_type = spec.get("event_type", "state_changed")
    if event_type != "state_changed":
        if ctx.definition.tools.ha.level != "admin":
            raise ScopeDeniedError(
                "event_not_permitted",
                "only state subscriptions are permitted below the admin level",
            )
        return
    entity_ids = spec.get("entity_ids")
    if (
        not isinstance(entity_ids, list)
        or not entity_ids
        or not all(isinstance(e, str) for e in entity_ids)
    ):
        raise ScopeDeniedError(
            "invalid_arguments", "a state subscription needs a list of entity_ids"
        )
    check_read(scope_of(ctx, env), [str(e) for e in entity_ids])


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
        _check_spec(ctx, env, args.kind, args.spec)
        return Prepared(args.model_dump(), args)

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
        await _check_owner(ctx, env, args.kind, args.item_id)
        _check_spec(ctx, env, args.kind, args.spec)
        return Prepared(args.model_dump(), args)

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
)
