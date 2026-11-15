"""Rule context items (design section 10.5).

The runtime collects the context items of a rule before its action runs.
Each item goes through the tool executor, so the scope check, the audit
log and the approval rule apply as for an agent turn:

- ``history``: ``ha.get_statistics``, in the read scope.
- ``action_response``: ``ha.call_service`` with ``return_response``. The
  service must only return data, pass the service call check and need no
  approval.
- ``store_file``: ``knowledge.read``, in an attached store.
- ``invocation_history``: a query of the agent's own invocations from the
  same source. No tool.

An item with ``cache_seconds`` keeps its value for that time. The cache key
holds the agent ID, the definition version and the item. On a cache hit, the
runtime runs the scope check of the item again before it uses the value, so
a value never outlives the scope that permitted it.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from hagent_app.rules.checks import _check_store, check_action_response
from hagent_app.rules.spec import (
    ActionResponseItem,
    ContextItem,
    HistoryItem,
    InvocationHistoryItem,
    StoreFileItem,
)
from hagent_app.storage import Database
from hagent_app.tools.base import ToolContext
from hagent_app.tools.executor import ToolExecutor
from hagent_app.tools.ha_tools import scope_of
from hagent_app.tools.scope import ScopeDeniedError, check_read


class RuleFailure(Exception):  # noqa: N818 - a rule outcome, not a bug
    """A part of the rule failed. The rule records an error."""


class ContextCollector:
    """Collects context items, with a bounded cache."""

    def __init__(
        self,
        db: Database,
        *,
        max_entries: int = 1000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._db = db
        self._max = max_entries
        self._clock = clock
        self._cache: OrderedDict[str, tuple[float, Any]] = OrderedDict()

    async def collect(
        self,
        ctx: ToolContext,
        executor: ToolExecutor,
        items: list[ContextItem],
        source_key: str,
    ) -> dict[str, Any]:
        """Collect every item. Raise ``RuleFailure`` or ``ScopeDeniedError``."""
        values: dict[str, Any] = {}
        for item in items:
            values[item.name] = await self._one(ctx, executor, item, source_key)
        return values

    def _key(self, ctx: ToolContext, item: ContextItem, source_key: str) -> str:
        data = item.model_dump(mode="json", exclude={"cache_seconds", "name"})
        if isinstance(item, InvocationHistoryItem):
            data["source_key"] = source_key
        return json.dumps(
            [ctx.agent.id, ctx.agent.version, data], sort_keys=True, default=str
        )

    async def _one(
        self,
        ctx: ToolContext,
        executor: ToolExecutor,
        item: ContextItem,
        source_key: str,
    ) -> Any:
        key = self._key(ctx, item, source_key)
        now = self._clock()
        cached = self._cache.get(key)
        if cached is not None and item.cache_seconds and cached[0] > now:
            self._recheck(ctx, executor, item)
            return cached[1]
        value = await self._fetch(ctx, executor, item, source_key)
        if item.cache_seconds:
            self._cache[key] = (now + item.cache_seconds, value)
            self._cache.move_to_end(key)
            while len(self._cache) > self._max:
                self._cache.popitem(last=False)
        return value

    def _recheck(
        self, ctx: ToolContext, executor: ToolExecutor, item: ContextItem
    ) -> None:
        """The scope check of a cached item. It never reaches HA or a store."""
        env = executor.env
        if isinstance(item, HistoryItem):
            if not ctx.definition.tools.enabled("ha"):
                raise ScopeDeniedError("tool_off", "the ha tools are off")
            check_read(scope_of(ctx, env), item.entity_ids)
        elif isinstance(item, ActionResponseItem):
            check_action_response(ctx, env, item)
        elif isinstance(item, StoreFileItem):
            _check_store(ctx, item)

    async def _fetch(
        self,
        ctx: ToolContext,
        executor: ToolExecutor,
        item: ContextItem,
        source_key: str,
    ) -> Any:
        if isinstance(item, HistoryItem):
            start = datetime.now(UTC) - timedelta(hours=item.hours)
            return await _call(
                executor,
                ctx,
                item.name,
                "ha.get_statistics",
                {
                    "statistic_ids": item.entity_ids,
                    "start": start.isoformat(),
                    "period": item.period,
                },
            )
        if isinstance(item, ActionResponseItem):
            # The extra checks first: only a data action, and no approval.
            check_action_response(ctx, executor.env, item)
            domain, service = item.service.split(".", 1)
            arguments: dict[str, Any] = {
                "domain": domain,
                "service": service,
                "return_response": True,
            }
            if item.target is not None:
                arguments["target"] = item.target
            if item.data is not None:
                arguments["data"] = item.data
            data = await _call(executor, ctx, item.name, "ha.call_service", arguments)
            return data.get("response") if isinstance(data, Mapping) else None
        if isinstance(item, StoreFileItem):
            arguments = {"store_id": item.store_id, "path": item.path}
            if item.section is not None:
                arguments["section"] = item.section
            return await _call(executor, ctx, item.name, "knowledge.read", arguments)
        return await self._invocations(ctx, item, source_key)

    async def _invocations(
        self, ctx: ToolContext, item: InvocationHistoryItem, source_key: str
    ) -> list[dict[str, Any]]:
        agent_id = ctx.agent.id
        current = ctx.invocation_id or ""

        def run(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute(
                "SELECT created_at, finished_at, status, path, rule_id,"
                " rule_version, request, result FROM invocations"
                " WHERE agent_id = ? AND source_key = ? AND dry_run = 0"
                " AND id != ? AND status IN ('ok', 'fallback', 'error')"
                " ORDER BY created_at DESC LIMIT ?",
                (agent_id, source_key, current, item.limit),
            ).fetchall()
            found = []
            for row in rows:
                request = json.loads(row["request"])
                found.append(
                    {
                        "created_at": row["created_at"],
                        "finished_at": row["finished_at"],
                        "status": row["status"],
                        "path": row["path"],
                        "rule_id": row["rule_id"],
                        "rule_version": row["rule_version"],
                        "data": request.get("data") or {},
                        "result": (
                            None if row["result"] is None else json.loads(row["result"])
                        ),
                    }
                )
            return found

        return await self._db.run(run)


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
