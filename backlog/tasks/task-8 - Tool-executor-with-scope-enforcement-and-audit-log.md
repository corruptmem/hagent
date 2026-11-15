---
id: TASK-8
title: Tool executor with scope enforcement and audit log
status: In Progress
assignee:
  - '@claude'
created_date: '2026-11-14 23:03'
updated_date: '2026-11-15 00:35'
labels:
  - app
  - security
dependencies:
  - TASK-6
  - TASK-7
references:
  - docs/design.md
priority: high
type: feature
ordinal: 8000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Build the tool executor and the scope check that every tool call passes, for the ha, knowledge, memory and self tool groups. The app has admin access to HA, so this check is the only barrier between an agent and full access.

Design: docs/design.md sections 8 and 19.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 read and control scopes resolve area, label, device and floor targets to entity IDs before the check
- [x] #2 Entity IDs inside service data are checked, not only targets
- [x] #3 Indirect services (script, automation, scene, generic homeassistant, shell_command, rest_command, python_script, pyscript, hassio, notify) are denied unless the scope names them
- [x] #4 admin is a blanket scope with no narrower form
- [x] #5 Scopes are re-resolved when HA registries change, and a growing scope sends an HA notification
- [x] #6 Every call that changes HA is written to the audit log; admin changes keep the previous version
- [x] #7 For every tool, a test proves that an out-of-scope call never reaches HA or the store
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Package app/src/hagent_app/tools/. Pure, synchronous scope checker: registry snapshot + agent snapshot + call -> denial or rewritten call. The rewritten call names explicit entity IDs, so HA acts only on checked entities.
2. Registry: load entity, device, area, floor and label registries and get_services over WebSocket. Mark stale on disconnect and on *_registry_updated events; reload in a task. Fail closed while stale.
3. Control rules: lift target keys out of service_data; check nested known entity and device IDs; deny templates, the all/none tokens without scope.all, unknown target keys, unknown services, indirect services that the scope does not name, and calls with zero resolved entities unless services names the service. Outputs are a short form of control.
4. Audit table (migration 0200): a pending row before each HA change, then the result. No HA call if the insert fails. Denied calls of every tool also go to the table. Admin changes keep the previous version: a state snapshot for service calls, and readers for an allowlist of WebSocket config commands.
5. Executor: tool registry with pydantic argument models; group and level gate; approval hook that denies when no gate exists; dry-run mode. Tools receive AgentReader snapshots only.
6. Store protocols for knowledge, memory and self, with fakes. agent_id comes from the invocation context. Attachment and ownership checks.
7. Scope monitor: re-resolve every agent scope after a registry reload, persist the resolved set per agent version, send an HA notification and a change event when a scope grows.
8. Tests with FakeHA (registry handlers added): a table of out-of-scope cases for every registered tool, asserting on FakeHA.commands and the fake store logs. Tests that no tool reaches AgentService or agents.store.
9. Wire ToolsComponent into default_components; document in app/README.md and docs/api.md.

10. After review: check group members, entity IDs in data keys, safe knowledge paths, no approval request in dry runs, final audit status on cancel.

11. Review-8 fixes. Registry: keep the get_services target filters and the declared fields (sections flattened). Scope check: a call with target entities needs a declared entity target that includes each entity domain. An explicit entity of another domain needs the exact service name in services. Outputs never permit a cross-domain service. A data key outside the declared fields is denied. Missing metadata denies. INDIRECT_DOMAINS gets recorder, zwave_js, zha, matter, knx, mqtt, group, logger, system_log, backup and hagent.

12. group.set and group.remove: object_id maps to group.<object_id>, which must be in the control scope.

13. A state cache that is not ready or is stale denies call_service and the read tools that use the cache (states_unavailable).

14. self.create and self.update: strict subscription model (extra=forbid), entities checked against the read scope. Rule and schedule kinds return not_supported until task-13 and task-15 add validated models.

15. Scope monitor stores a grown baseline only after the notification succeeds. admin_entities expands groups and scans data; a failed scan marks the call for approval when approval.entities is set. Executor catch-all denies with check_failed. ENTITY_ID and _NAME use fullmatch.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Decisions:
- Floors: the lead approved floors in HAScope (agents/definition.py), with docs/api.md and definition tests. Floors resolve in scopes and in call targets.
- The scope check (tools/scope.py) is pure and synchronous. It rewrites each permitted call to explicit entity IDs. HA therefore acts only on checked entities, also if the registry changes after the check.
- Indirect targets (area, device, floor, label) follow HA target resolution: no hidden, disabled or config/diagnostic entities, entity area overrides device area, and only entities of the service domain.
- Zero-target rule: a call that resolves to no entity needs its exact name in tools.ha.services. This stops mqtt.publish, recorder.purge, conversation.process and similar services that the indirect list does not name.
- Templates in service data are denied. Data values and keys are scanned for known entity and device IDs, lower-cased and split at commas like cv.entity_ids. Nested target keys other than entity_id are denied. The all/none tokens need scope all=true.
- Group entities: members from the entity_id state attribute (nested, depth 5) must be in the scope.
- Outputs are a short form of control, without indirect services and without mixed targets.
- Registry: HARegistry loads entity, device, area, floor and label registries and get_services. It is stale before the first load, after a disconnect, and from an update event until the reload ends. Control checks and registry-based read scopes fail closed while stale.
- Audit (migration 0200): a pending row before each HA change; no HA call if the insert fails; the final status after the call, also on cancel. Denied calls of every tool are recorded.
- Admin: blanket for ha.call_service, with a state snapshot of named entities as the previous version. Admin WebSocket commands come from two allowlists (tools/admin.py). Each change command has a previous-version reader; a reader failure denies the change.
- Approval hook: ApprovalGate protocol. With no gate, a call marked for approval is denied. A dry run returns needs_approval and sends no request. Service and entity approval applies to writing tools only.
- memory and self tools bind the agent ID from the invocation. Ownership checks before changes. State subscriptions must pass the read scope; other event types need admin.
- Scope monitor: re-resolves every scope after each registry reload, stores the result per agent version in tool_scope_snapshots, and on growth within the same version sends persistent_notification.create and a tools/scope_grown change event.

Known limits:
- Admin changes to automation, script and scene configuration need the HA REST API. The app has no REST client, so the admin tools cannot make those changes.
- Integration-native groups and template entities that run actions are not visible to the group check.
- Read tools do not expand groups: a group state shows an aggregate of its members.

Evidence: mise run check passes (ruff, mypy strict, 300 tests). Tool tests: 222 passed in test_tool_scope.py, test_tool_executor.py, test_tool_monitor.py, test_tools_no_writer.py and test_agent_definition.py.
- AC1: test_scope_resolves_registry_targets, test_floor_target_resolves_to_entities, test_area_target_becomes_explicit_entities, test_label_and_device_targets_resolve.
- AC2: test_out_of_scope_calls_are_denied (data cases), test_entity_ids_in_data_keys_are_checked, test_target_keys_in_service_data_are_lifted.
- AC3: test_every_indirect_service_is_denied_unless_named (all ten domains), test_named_indirect_service_is_permitted.
- AC4: test_admin_passes_unchanged; definition tests reject a scope or services for admin.
- AC5: test_stale_registry_denies_and_reload_resolves_again, test_growth_sends_a_notification_and_an_event, test_growth_while_the_app_was_stopped_is_found.
- AC6: test_permitted_service_call_sends_explicit_entities, test_admin_change_keeps_the_previous_version, test_admin_service_call_keeps_previous_states, test_no_ha_call_when_the_audit_write_fails, test_cancelled_call_finishes_its_audit_row.
- AC7: test_out_of_scope_call_never_reaches_ha_or_a_store (42 cases; asserts FakeHA.ha_commands() and fake store calls are empty), test_every_tool_has_an_out_of_scope_case.
- Task-7 AC #4 completion: test_tools_package_imports_no_definition_writer and test_executor_reaches_no_agent_service.
<!-- SECTION:NOTES:END -->

## Comments

<!-- COMMENTS:BEGIN -->
author: @claude
created: 2026-11-15 00:16
---
Proposed follow-ups (not created): (1) task-13: rule writes change HA, so they must run through ToolExecutor or check_service_call to get the scope check and the audit row; self.create does not check rule specs. (2) task-15: check subscriptions again at delivery, because a scope can shrink after a subscription exists; enforce max_schedules and min_interval. (3) An HA REST client for admin changes to automation, script and scene configuration, with previous versions. (4) A management API route for the audit log and for effective scopes (design 7.2, Inspect > Permissions and Changes to HA).
---
<!-- COMMENTS:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Added the tools package: a pure scope check that resolves area, device, floor and label targets and rewrites each call to explicit entity IDs; the HA registry snapshot with fail-closed staleness; the tool executor with the ha, knowledge, memory and self groups, an approval hook and dry runs; the audit log (migration 0200) with previous versions for admin changes; and the scope monitor that notifies HA when a scope grows. Added floors to HAScope with the lead's approval. Store protocols let task-10, task-11 and task-13 to task-15 plug in. Verified with mise run check (300 tests), including an out-of-scope table for every tool that asserts FakeHA and the fake stores receive nothing.
<!-- SECTION:FINAL_SUMMARY:END -->
