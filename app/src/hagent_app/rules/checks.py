"""Scope checks of rule specs (design sections 8, 10.1 and 10.5).

``check_rule_spec`` is the ``SPEC_CHECKS`` entry for the kind ``rule``. The
``self.create`` and ``self.update`` tools call it before they reach the
store. The rule stage calls ``check_writes`` again before each run, and
runs every context item and write through the tool executor, so a scope
that shrank after the rule was written takes effect at once.

Checks at create time:

- The spec validates against ``RuleSpec``. Unknown keys fail.
- Each template is in the supported subset (``check_template``).
- A decision action needs a decision model in the definition.
- A ``history`` item names entities in the read scope.
- An ``action_response`` item names a service that only returns data, and
  the call passes ``check_service_call`` and needs no approval.
- A ``store_file`` item names an attached store and a safe path.
- Each write passes ``check_service_call`` with placeholder data values,
  and needs no approval. Rules cannot wait for approval.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from hagent_app.rules.spec import (
    ActionResponseItem,
    DecisionAction,
    HistoryItem,
    RuleSpec,
    StoreFileItem,
    WriteSpec,
)
from hagent_app.rules.templates import TemplateError, check_template
from hagent_app.tools.base import Prepared, ToolContext, ToolEnv
from hagent_app.tools.executor import needs_approval
from hagent_app.tools.ha_tools import (
    CallService,
    group_members,
    require_states,
    scope_of,
    snapshot_or_none,
)
from hagent_app.tools.registry import RegistryUnavailableError
from hagent_app.tools.scope import (
    ScopeDeniedError,
    admin_entities,
    check_read,
    check_service_call,
)

_CALL_SERVICE = CallService()


def parse_spec(spec: Any) -> RuleSpec:
    """Validate a spec. Raise ``ScopeDeniedError`` with ``invalid_spec``."""
    try:
        return RuleSpec.model_validate(spec)
    except ValidationError as err:
        parts = []
        for item in err.errors(include_url=False, include_context=False)[:5]:
            loc = ".".join(str(p) for p in item["loc"]) or "spec"
            parts.append(f"spec.{loc}: {item['msg']}")
        raise ScopeDeniedError("invalid_spec", "; ".join(parts)) from err


def check_rule_spec(ctx: ToolContext, env: ToolEnv, spec: Any) -> dict[str, Any]:
    """Check a rule spec for ``self.create`` and ``self.update``."""
    rule = parse_spec(spec)
    for place, source in rule.templates():
        try:
            check_template(source)
        except TemplateError as err:
            raise ScopeDeniedError("invalid_template", f"{place}: {err}") from err
    if (
        isinstance(rule.action, DecisionAction)
        and ctx.definition.models.decision is None
    ):
        raise ScopeDeniedError(
            "no_decision_model", "a decision action needs models.decision"
        )
    for item in rule.context:
        if isinstance(item, HistoryItem):
            check_read(scope_of(ctx, env), item.entity_ids)
        elif isinstance(item, ActionResponseItem):
            check_action_response(ctx, env, item)
        elif isinstance(item, StoreFileItem):
            _check_store(ctx, item)
    check_writes(ctx, env, rule)
    return rule.to_data()


def _check_store(ctx: ToolContext, item: StoreFileItem) -> None:
    # Imported here: store_tools loads this module lazily (import cycle).
    from hagent_app.tools.store_tools import _attachment, _safe_path  # noqa: PLC0415

    if not ctx.definition.tools.enabled("knowledge"):
        raise ScopeDeniedError("tool_off", "the knowledge tools are off")
    _attachment(ctx, item.store_id)
    try:
        _safe_path(item.path)
    except ValueError as err:
        raise ScopeDeniedError("invalid_spec", str(err)) from err


def _prepared(  # noqa: PLR0913 - mirrors check_service_call
    ctx: ToolContext,
    env: ToolEnv,
    service: str,
    *,
    data: dict[str, Any] | None,
    target: dict[str, Any] | None,
    return_response: bool = False,
) -> Prepared:
    """Run the service call check and build the request for the approval
    check, as ``ha.call_service`` does."""
    if not _CALL_SERVICE.available(ctx.definition):
        raise ScopeDeniedError("level", "the agent cannot call HA services")
    scope = scope_of(ctx, env)
    require_states(env)
    reg = snapshot_or_none(env)
    known = frozenset(s.entity_id for s in env.states)
    members = group_members(env)
    domain, name = service.split(".", 1)
    try:
        call = check_service_call(
            scope,
            reg,
            known,
            domain,
            name,
            data=data,
            target=target,
            return_response=return_response,
            group_members=members,
        )
    except RegistryUnavailableError as err:
        raise ScopeDeniedError("registry_unavailable", str(err)) from err
    entities = call.entities | call.data_entities
    approval_required = False
    if call.path == "admin":
        found = admin_entities(reg, known, data, target, members)
        entities = found.entities
        approval_required = not found.complete and bool(
            ctx.definition.approval.entities
        )
    return Prepared(
        call.request(),
        call,
        services=frozenset({call.service_name}),
        entities=entities,
        approval_required=approval_required,
    )


def _deny_approval(ctx: ToolContext, prepared: Prepared, what: str) -> None:
    if needs_approval(ctx.definition, _CALL_SERVICE, prepared):
        raise ScopeDeniedError(
            "approval_required",
            f"{what} needs approval, and rules cannot wait for approval",
        )


def check_action_response(
    ctx: ToolContext, env: ToolEnv, item: ActionResponseItem
) -> Prepared:
    """Check an ``action_response`` context item."""
    domain, name = item.service.split(".", 1)
    reg = snapshot_or_none(env)
    if reg is None:
        raise ScopeDeniedError(
            "registry_unavailable", "the HA registry data is not current"
        )
    info = reg.info(domain, name)
    if info is None or not info.response_only:
        raise ScopeDeniedError(
            "not_response_only",
            f"{item.service} is not an action that only returns data",
        )
    prepared = _prepared(
        ctx,
        env,
        item.service,
        data=None if item.data is None else dict(item.data),
        target=None if item.target is None else dict(item.target),
        return_response=True,
    )
    _deny_approval(ctx, prepared, f"context item {item.name}")
    return prepared


def check_write(ctx: ToolContext, env: ToolEnv, write: WriteSpec) -> Prepared:
    """Check one write with placeholder data values.

    The data values are templates, and the service call check denies
    template text. The check therefore uses ``None`` for each value. The
    rule stage checks the rendered values again through the executor.
    """
    prepared = _prepared(
        ctx,
        env,
        write.service,
        data=dict.fromkeys(write.data),
        target={"entity_id": [write.entity_id]},
    )
    _deny_approval(ctx, prepared, f"the write to {write.entity_id}")
    return prepared


def check_writes(ctx: ToolContext, env: ToolEnv, rule: RuleSpec) -> None:
    """Check every write of the rule."""
    for write in rule.writes:
        check_write(ctx, env, write)
