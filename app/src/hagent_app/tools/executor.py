"""The tool executor: the one path from an agent turn to HA and the stores.

``ToolExecutor.execute`` runs these steps for each call, in this order:

1. Find the tool. Check that the definition turns on the tool.
2. Validate the arguments. Unknown arguments fail.
3. ``authorize``: the scope check. It returns a prepared request.
4. A dry run of a call that writes or needs approval stops here. It records
   the call and returns the prepared request, with no approval request.
5. The approval check. If the definition marks the call for approval, the
   approval gate must accept it. With no gate, the executor denies the call
   (task-16 supplies the gate). The gate can return ``ApprovalOutcome`` with
   a denial code, for example ``approval_timeout``. If the invocation is
   cancelled during the wait, the executor records ``approval_cancelled``.
6. For a call that changes HA: write the audit row with status
   ``pending``. If the write fails, stop.
7. ``run`` on the prepared request.
8. For a call that changes HA: write the final status, also when the
   invocation is cancelled.

The executor writes every denied call to the audit log. It never raises
for a denial or a tool error: it returns a ``ToolResult`` for the model.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Protocol

from pydantic import ValidationError

from hagent_app.agents.definition import AgentDefinition
from hagent_app.ha.client import HAError
from hagent_app.tools.audit import AuditCall, AuditLog
from hagent_app.tools.base import (
    Prepared,
    StateView,
    Tool,
    ToolContext,
    ToolEnv,
    ToolResult,
    ToolSpec,
    ToolUnavailableError,
)
from hagent_app.tools.ha_tools import HA_TOOLS
from hagent_app.tools.registry import RegistryUnavailableError
from hagent_app.tools.scope import ScopeDeniedError
from hagent_app.tools.store_tools import STORE_TOOLS
from hagent_app.tools.stores import StoreError

_LOG = logging.getLogger(__name__)

ALL_TOOLS: tuple[Tool, ...] = HA_TOOLS + STORE_TOOLS


APPROVAL_DENIAL_CODES = frozenset(
    {
        "approval_rejected",
        "approval_timeout",
        "approval_unavailable",
        "approval_stale",
    }
)


@dataclass(frozen=True, slots=True)
class ApprovalOutcome:
    """The answer of an approval gate.

    ``accepted`` is True only if an approver accepted the call. For a
    denial, ``code`` is one of ``APPROVAL_DENIAL_CODES``. The executor maps
    any other code to ``approval_rejected``.
    """

    accepted: bool
    code: str = "approval_rejected"
    message: str = "the approver did not accept the call"


class ApprovalGate(Protocol):
    """The hook for task-16: hold a call until a person accepts or rejects it."""

    async def request(
        self, ctx: ToolContext, tool: str, prepared: Prepared
    ) -> bool | ApprovalOutcome:
        """Return True, or an accepted outcome, if an approver accepts the call."""
        ...


def _approval_outcome(answer: object) -> ApprovalOutcome:
    """Normalise a gate answer. Anything but an explicit accept is a denial."""
    if answer is True:
        return ApprovalOutcome(accepted=True)
    if isinstance(answer, ApprovalOutcome):
        if answer.accepted is True:
            return answer
        if answer.code in APPROVAL_DENIAL_CODES:
            return answer
        return ApprovalOutcome(False, "approval_rejected", answer.message)
    return ApprovalOutcome(accepted=False)


def needs_approval(definition: AgentDefinition, tool: Tool, prepared: Prepared) -> bool:
    """True if the definition marks this call for approval (design section 8)."""
    approval = definition.approval
    refs = {tool.group, f"{tool.group}.*", tool.name}
    if refs & set(approval.tools):
        return True
    if not tool.writes:
        # Service and entity approval applies to changes only.
        return False
    if prepared.approval_required:
        return True
    for name in prepared.services:
        domain = name.split(".", 1)[0]
        if name in approval.services or f"{domain}.*" in approval.services:
            return True
    return bool(prepared.entities & set(approval.entities))


def _validation_message(err: ValidationError) -> str:
    parts = []
    for item in err.errors(include_url=False, include_context=False)[:5]:
        loc = ".".join(str(p) for p in item["loc"]) or "arguments"
        parts.append(f"{loc}: {item['msg']}")
    return "; ".join(parts)


class ToolExecutor:
    """Run tool calls for agent turns, with the scope check first."""

    def __init__(
        self,
        env: ToolEnv,
        audit: AuditLog,
        *,
        approval: ApprovalGate | None = None,
        tools: Iterable[Tool] = ALL_TOOLS,
    ) -> None:
        self._env = env
        self._audit = audit
        self._approval = approval
        self._tools = {tool.name: tool for tool in tools}

    def with_states(self, states: StateView) -> ToolExecutor:
        """A copy of this executor that reads ``states`` in place of the
        state cache. A test invocation uses it for its sample states. The
        scope check, the audit log and the approval gate stay the same."""
        copy = ToolExecutor(
            replace(self._env, states=states), self._audit, approval=self._approval
        )
        copy._tools = self._tools
        return copy

    def without_approval(self) -> ToolExecutor:
        """A copy of this executor with no approval gate.

        The rule stage (task-13) uses it: rules cannot wait for approval
        (design section 8), so a call that needs approval is denied with
        ``approval_unavailable`` and recorded in the audit log.
        """
        copy = ToolExecutor(self._env, self._audit, approval=None)
        copy._tools = self._tools
        return copy

    @property
    def env(self) -> ToolEnv:
        """The services that the tools use. The rule stage reads the state
        cache and the registry from it for its checks."""
        return self._env

    @property
    def tool_names(self) -> list[str]:
        """The names of every tool that the executor knows."""
        return sorted(self._tools)

    def tools_for(self, definition: AgentDefinition) -> list[ToolSpec]:
        """The tools that the definition turns on, for the model."""
        return [
            ToolSpec(
                name=tool.name,
                group=tool.group,
                description=tool.description,
                parameters=tool.args_model.model_json_schema(),
            )
            for tool in self._tools.values()
            if tool.available(definition)
        ]

    async def execute(  # noqa: PLR0911, PLR0912 - one linear pipeline
        self, ctx: ToolContext, name: str, arguments: Mapping[str, Any]
    ) -> ToolResult:
        """Run one tool call. Never raise for a denial or a tool error."""
        tool = self._tools.get(name)
        call = AuditCall(
            agent_id=ctx.agent.id,
            agent_version=ctx.agent.version,
            invocation_id=ctx.invocation_id,
            tool=name,
            level=tool.level(ctx.definition) if tool else "unknown",
            request=dict(arguments) if isinstance(arguments, Mapping) else None,
        )
        if tool is None:
            return await self._deny(call, "unknown_tool", f"no tool named {name!r}")
        if not tool.available(ctx.definition):
            return await self._deny(
                call, "tool_off", f"the definition does not turn on {name}"
            )
        try:
            args = tool.args_model.model_validate(arguments)
        except ValidationError as err:
            return await self._deny(call, "invalid_arguments", _validation_message(err))

        # The scope check.
        try:
            prepared = await tool.authorize(ctx, self._env, args)
        except ScopeDeniedError as err:
            return await self._deny(call, err.code, err.message)
        except RegistryUnavailableError as err:
            return await self._deny(call, "registry_unavailable", str(err))
        except ToolUnavailableError as err:
            return await self._deny(call, "unavailable", str(err))
        except (HAError, TimeoutError) as err:
            return await self._deny(call, "ha_error", f"the check failed: {err}")
        except Exception:
            # Fail closed, and record the attempt.
            _LOG.exception("the check of tool %s failed", name)
            return await self._deny(
                call, "check_failed", "the check failed because of an internal error"
            )
        call = AuditCall(
            call.agent_id,
            call.agent_version,
            call.invocation_id,
            call.tool,
            call.level,
            dict(prepared.request),
        )

        approval_needed = needs_approval(ctx.definition, tool, prepared)

        # A dry run stops here for a call that writes or needs approval. It
        # sends no approval request, because a test invocation must not
        # reach approvers.
        if ctx.dry_run and (tool.writes or approval_needed):
            return await self._dry_run(call, tool, prepared, approval_needed)

        # The approval hook.
        if approval_needed:
            denied = await self._approve(ctx, call, name, prepared)
            if denied is not None:
                return denied

        audit_id = None
        if tool.changes_ha:
            # No HA change without its audit row.
            try:
                audit_id = await self._audit.begin(
                    call,
                    previous=prepared.previous,
                    previous_note=prepared.previous_note,
                )
            except Exception:
                _LOG.exception("cannot write the audit log; the call stops")
                return ToolResult(
                    False, error_code="audit_failed", error="the audit log failed"
                )

        try:
            data = await tool.run(ctx, self._env, prepared)
        except asyncio.CancelledError:
            # HA can have acted already. Record the end, then stop.
            await asyncio.shield(
                self._finish(audit_id, "error", "the invocation was cancelled")
            )
            raise
        except ToolUnavailableError as err:
            await self._finish(audit_id, "error", str(err))
            return ToolResult(False, error_code="unavailable", error=str(err))
        except (HAError, TimeoutError) as err:
            await self._finish(audit_id, "error", str(err))
            return ToolResult(False, error_code="ha_error", error=str(err))
        except StoreError as err:
            await self._finish(audit_id, "error", err.message)
            return ToolResult(False, error_code=err.code, error=err.message)
        except Exception as err:
            _LOG.exception("tool %s failed", name)
            await self._finish(audit_id, "error", repr(err))
            return ToolResult(False, error_code="tool_error", error=str(err))
        context_id = data.get("context_id") if isinstance(data, dict) else None
        await self._finish(
            audit_id, "ok", None, context_id if isinstance(context_id, str) else None
        )
        return ToolResult(True, data, audit_id=audit_id)

    async def _approve(
        self, ctx: ToolContext, call: AuditCall, name: str, prepared: Prepared
    ) -> ToolResult | None:
        """Ask the gate. Return a denial, or ``None`` if an approver accepted."""
        if self._approval is None:
            return await self._deny(
                call,
                "approval_unavailable",
                "the call needs approval, and approvals are not available",
            )
        try:
            outcome = _approval_outcome(
                await self._approval.request(ctx, name, prepared)
            )
        except asyncio.CancelledError:
            # Record the held call in the denied-calls log, then stop.
            await asyncio.shield(
                self._deny(
                    call,
                    "approval_cancelled",
                    "the invocation was cancelled during the approval",
                )
            )
            raise
        except Exception:
            _LOG.exception("approval request failed")
            outcome = ApprovalOutcome(
                False, "approval_unavailable", "the approval request failed"
            )
        if not outcome.accepted:
            return await self._deny(call, outcome.code, outcome.message)
        return None

    async def _dry_run(
        self, call: AuditCall, tool: Tool, prepared: Prepared, approval_needed: bool
    ) -> ToolResult:
        audit_id = None
        if tool.changes_ha:
            try:
                audit_id = await self._audit.begin(
                    call,
                    previous=prepared.previous,
                    previous_note=prepared.previous_note,
                )
                await self._audit.finish(audit_id, "dry_run")
            except Exception:
                _LOG.exception("cannot write the audit log")
        data = {
            "dry_run": True,
            "needs_approval": approval_needed,
            "request": dict(prepared.request),
        }
        return ToolResult(True, data, audit_id=audit_id)

    async def _finish(
        self,
        audit_id: int | None,
        status: Any,
        error: str | None,
        context_id: str | None = None,
    ) -> None:
        if audit_id is None:
            return
        try:
            await self._audit.finish(
                audit_id, status, error=error, context_id=context_id
            )
        except Exception:
            _LOG.exception("cannot finish audit row %s", audit_id)

    async def _deny(self, call: AuditCall, code: str, message: str) -> ToolResult:
        audit_id = None
        try:
            audit_id = await self._audit.denied(call, code, message)
        except Exception:
            _LOG.exception("cannot record a denied call")
        return ToolResult(False, error_code=code, error=message, audit_id=audit_id)
