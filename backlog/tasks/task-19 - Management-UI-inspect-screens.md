---
id: TASK-19
title: 'Management UI: inspect screens'
status: In Progress
assignee:
  - '@claude'
created_date: '2026-11-14 23:04'
updated_date: '2026-11-15 02:20'
labels:
  - ui
dependencies:
  - TASK-18
  - TASK-14
  - TASK-15
  - TASK-16
references:
  - docs/design.md
priority: medium
type: feature
ordinal: 19000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Build the inspect area: invocation list and full traces, history search, rules with versions, backtests and trials, schedules and subscriptions, permissions, the HA audit log, cost and approvals, with live updates over WebSocket.

Design: docs/design.md section 7.2 (Inspect).
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Invocation traces show inputs, rule or model messages, every tool call with its scope decision, compactions, result and cost
- [ ] #2 Rules show versions, statistics per version, backtest results and trial agreement, with turn off, delete and roll back
- [ ] #3 Permissions show effective scopes resolved to entity IDs and denied calls
- [ ] #4 Cost shows totals by agent, day and path, including cached tokens and embeddings
- [ ] #5 Lists update live when new invocations arrive
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Back end: new read-only package app/src/hagent_app/inspect/ (InspectComponent + InspectApi), wired in default_components() before http. Routes under api/inspect/: invocations list (filters agent, source, path, status; q = history search over request, result and turn input/result), invocation trace (invocation, turn with messages, tool calls joined with audit decision, compactions, provider calls, trials; rule_id and rule_version only - the UI reads the spec through the rules API), audit log (Changes to HA, with previous versions), agent permissions (agent_scope resolved to entity IDs with the state cache, tools_for list, knowledge attachments, denied calls), schedules and subscriptions (items with last run/event), cost (totals by agent, day and path; cache tokens; embeddings). Every agent route filters by agent ID. Payloads pass redact() for approval action IDs. Never touch rules tables.
2. Live updates: register router result and activity listeners; publish topic invocations. Document in docs/api.md.
3. Rule routes (approved by lead): POST api/agents/{id}/rules/{rule_id}/state and DELETE api/agents/{id}/rules/{rule_id} in backtest/api.py, using RuleStore.set_enabled/delete; events rules/updated and rules/deleted; tests for own-rule isolation, deleted rule 404, and an off rule no longer answers.
4. pytest tests: each route, agent isolation, secret test (API key and link secret never appear), doc test.
5. Front end: Inspect area with screens Invocations (+ search), Trace, Rules, Schedules, Permissions, Changes, Cost, Approvals. events.ts invalidates invocations, rules, approvals, tools. vitest test per screen, including a live-update test. Update frontend/README.md.
6. mise run check; notes, summary, report to t3.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Back end done: inspection/ package with routes under api/inspect (invocations + history search, trace, audit, schedules, cost, agent permissions), invocations change events from router listeners, rule state and delete routes in backtest/api.py. 1066 app tests pass.
<!-- SECTION:NOTES:END -->
