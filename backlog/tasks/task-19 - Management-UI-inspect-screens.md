---
id: TASK-19
title: 'Management UI: inspect screens'
status: In Progress
assignee:
  - '@claude'
created_date: '2026-11-14 23:04'
updated_date: '2026-11-15 02:35'
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
- [x] #1 Invocation traces show inputs, rule or model messages, every tool call with its scope decision, compactions, result and cost
- [x] #2 Rules show versions, statistics per version, backtest results and trial agreement, with turn off, delete and roll back
- [x] #3 Permissions show effective scopes resolved to entity IDs and denied calls
- [x] #4 Cost shows totals by agent, day and path, including cached tokens and embeddings
- [x] #5 Lists update live when new invocations arrive
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

AC evidence (mise run check exit 0: app 1103 passed, frontend 62 passed, integration 43 + 10 passed):
- AC1 traces: app test_inspect.py::test_a_trace_shows_tool_calls_with_their_scope_decision (real executor calls; allowed, denied and read decisions; compactions; cost with cached tokens) and ::test_a_rule_trace_has_the_rule_version_and_its_trial; frontend Trace.test.tsx (inputs, model messages, tool calls with scope decisions, compactions, result, cost; rule spec through the rule routes).
- AC2 rules: app test_backtest.py::test_the_user_turns_a_rule_off_and_on (an off rule no longer answers; the agent turn answers), ::test_the_user_cannot_activate_a_draft_rule, ::test_the_user_deletes_a_rule, ::test_state_and_delete_act_only_on_the_agents_own_rules; frontend Rules.test.tsx (versions, statistics per version, backtest and trial agreement, version specs, turn off, roll back, delete with confirmation).
- AC3 permissions: app ::test_permissions_resolve_scopes_to_entity_ids (area and domain scopes resolved to entity IDs, inputs in read scope, only own denied calls), ::test_admin_permissions_say_all_and_do_not_list_entities; frontend Permissions.test.tsx.
- AC4 cost: app ::test_cost_by_agent_day_and_path (agent, day, path, call type, cached tokens, embedding cost, day window); frontend Others.test.tsx 'cost'.
- AC5 live: app ::test_the_real_router_publishes_invocation_events (real RouterComponent: invocations/finished with the invocation ID, activity running 1 then 0); frontend Invocations.test.tsx 'updates the list live when a new invocation arrives' (FakeWebSocket event causes a refetch).
Decisions:
- POST .../rules/{rule_id}/state refuses active on a draft (409 rule_is_draft): the agent activates drafts after a backtest (design 10.7). Approved routes from the lead.
- A queued invocation sends no event until it starts (the router has no queue listener). Lists update at start and end.
- History search is a case-insensitive LIKE over invocation request/result/error and turn input/result, across agents. It does not use the per-agent vector index.
- Scope decision of a tool call comes from its audit row: the executor writes a row for each denial and write, so a call without a row passed the check.
- Package is named inspection, not inspect, to avoid the stdlib module name.
- The trace route is not keyed by agent: the UI is the admin view of all agents.
- Secrets: payloads pass scheduler.redact (approval action IDs); every response also replaces the stored API keys and the link secret with [redacted]. ::test_no_route_returns_a_secret seeds both into returned fields and fails without the scrub.
- Rules and approvals data are read only through their existing API routes; inspection/ never reads the rules tables.
Proposed follow-ups (not created): per-case backtest results view (GET .../backtests/{id} exists, unused by the UI); an invocation queue listener for a queued event; run a backtest from the UI.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Built the inspect area of the management UI (design 7.2). Back end: new read-only inspection/ package with routes under api/inspect for invocations with filters and history search, full traces, the audit log with previous versions, effective permissions with denied calls, schedules and subscriptions, and cost by agent, day, path and call type. It publishes invocations change events from the router listeners and scrubs secrets from every response. Added the approved rule routes POST .../rules/{rule_id}/state and DELETE .../rules/{rule_id}. Front end: Inspect screens (Invocations, Trace, Rules, Schedules, Permissions, Changes to HA, Cost, Approvals with answer) with live updates. Documented in docs/api.md, app/README.md and frontend/README.md. Verified with mise run check (exit 0) and the tests named in the notes.
<!-- SECTION:FINAL_SUMMARY:END -->
