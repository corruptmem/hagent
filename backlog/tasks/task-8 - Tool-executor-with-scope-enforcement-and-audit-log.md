---
id: TASK-8
title: Tool executor with scope enforcement and audit log
status: In Progress
assignee:
  - '@claude'
created_date: '2026-11-14 23:03'
updated_date: '2026-11-14 23:52'
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
- [ ] #1 read and control scopes resolve area, label, device and floor targets to entity IDs before the check
- [ ] #2 Entity IDs inside service data are checked, not only targets
- [ ] #3 Indirect services (script, automation, scene, generic homeassistant, shell_command, rest_command, python_script, pyscript, hassio, notify) are denied unless the scope names them
- [ ] #4 admin is a blanket scope with no narrower form
- [ ] #5 Scopes are re-resolved when HA registries change, and a growing scope sends an HA notification
- [ ] #6 Every call that changes HA is written to the audit log; admin changes keep the previous version
- [ ] #7 For every tool, a test proves that an out-of-scope call never reaches HA or the store
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
<!-- SECTION:PLAN:END -->
