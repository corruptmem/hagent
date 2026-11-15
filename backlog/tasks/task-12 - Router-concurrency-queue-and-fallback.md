---
id: TASK-12
title: 'Router, concurrency queue and fallback'
status: In Progress
assignee:
  - '@claude'
created_date: '2026-11-14 23:04'
updated_date: '2026-11-15 00:58'
labels:
  - app
dependencies:
  - TASK-10
references:
  - docs/design.md
priority: high
type: feature
ordinal: 12000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Build the router that handles every invocation: turned-off agents, the per-agent queue with HA automation modes, rule dispatch, agent turns and fallbacks. Persist the queue across app restarts.

Design: docs/design.md sections 9.1, 9.4, 9.6, 9.7 and 18.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 queued, single, restart and parallel modes behave as in section 9.6, with tests for each
- [x] #2 Assist conversations use their own queue per conversation
- [x] #3 Rule and definition changes take effect only between invocations
- [x] #4 The fallback (last_result, fixed, no_change, error) is returned for each cause in section 9.7, and history records the reason
- [x] #5 Queued invocations survive an app restart; running ones are recorded as failed and return the fallback
- [x] #6 Every invocation records path, result, cost, definition version and rule version
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Migrations 0600 (invocations table: queue rows and history) and 0601 (provider_calls.invocation_id + index).
2. providers: a ContextVar for the current invocation ID; Meter records it in provider_calls. Set max_retries=0 on the SDK clients so that each attempt is metered.
3. router/retry.py: RetryingModel (WrapperModel outside MeteredModel) with backoff for retryable errors only; wired through the TurnsComponent model factory in default_components.
4. router package: models (InvocationRequest, InvocationResult, stable error codes), store (SQLite), rules stage protocol with NoRules ('no rule matched'), fallback (last_result, fixed, no_change, error, with reasons), component (RouterComponent: submit/invoke/cancel, result listeners, per-queue lanes, modes queued/single/restart/parallel, Assist lane per conversation, snapshot taken at start).
5. Persistence: on start, running rows -> failed + fallback (app_restart); queued rows resume in the background after HA connects. On stop, running -> fallback (app_stopped); queued rows stay queued.
6. Cost: sum provider_calls by invocation_id after the turn.
7. POST api/agents/{id}/test: dry-run outside the queue, draft definition validation, sample states via a state overlay seam in tools and turns; planned and denied calls from the turn tool records. Update docs/api.md.
8. Tests with a fake turn runner (modes, Assist, fallback matrix, restart persistence) and the real runner with FunctionModel + Meter (cost link, retry). Register in default_components, update test_http_server, app/README.md.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Implementation (commits 34692f8, 74fe99c, 898f0b6):
- router/ package. RouterComponent.submit(request) returns an InvocationHandle; invoke() waits; cancel(id) stops a queued or running invocation; add_result_listener() gives each final result, also of resumed runs (for task-17 hagent_result). InvocationRequest carries source, text, data, conversation_id, result_schema and caller metadata (design 9.1).
- Lanes: one per agent, one per Assist conversation (key <agent>/assist:<conv>). Modes queued/single/restart/parallel as in design 9.6. queue_size caps waiting invocations (queued) and running invocations (parallel). Assist always uses queued mode in its conversation, with assist_queue_size.
- The router reads the agent snapshot when the invocation starts, not when it is queued. A turned-off or deleted agent is refused at both points.
- Rule stage seam: RuleStage protocol, NoRules returns no_rule_matched. Review, Assist and test invocations skip it. Task-13 sets RouterComponent.rules.
- Fallback: stop reasons map to budget, provider_unreachable, tool_call_limit, token_limit, request_limit, invalid_result, turn_failed; plus runtime_unavailable, app_restart, app_stopped. last_result uses the same source key, and ignores dry runs and fallback results.
- invocations table (0600) is the history and the queue. On start: running rows -> fallback app_restart; queued rows run again in order. On stop: running -> fallback app_stopped; queued rows stay queued.
- Cost: providers.meter.current_invocation (ContextVar) -> provider_calls.invocation_id (0601). The router sums cost when the invocation ends.
- Chat retry: RetryingModel wraps MeteredModel per request, never per turn. SDK retries off (max_retries=0), so each attempt is metered. Retryable: connection, timeout, 429, 5xx.
- Task-18 AC #5 is done: POST api/agents/{id}/test runs a dry-run turn outside the queue, with draft definitions and sample states (also for HA read tools). docs/api.md updated; the UI contract did not change (fields only added). The lead can close task-18.

Edits outside the router area (needed seams):
- providers/meter.py, providers/chat.py: the ContextVar and max_retries=0.
- tools/base.py, tools/executor.py, tools/__init__.py: StateView protocol and ToolExecutor.with_states.
- turns/runner.py, turns/component.py: states= argument of run_turn.
- agents/store.py, agents/service.py: AgentReader.check_draft (reads only); test_agent_reader.py interface set updated.
- Migration 0601 alters provider_calls (task-9 table) from the router range.

Deviations and limits:
- docs/api.md said store writes go to a discarded copy. The executor skips them in a dry run instead; the doc now says so.
- fixed fallback applies no writes: the definition has no writes field for it.
- The running summary update inherits the invocation ID, so its cost arrives after the result.
- A draft test records the current agent_version as base and caller.draft=true in the request JSON.
- Streamed requests are not retried.

Validation: mise run check passed (ruff, mypy strict, 435 pytest, frontend 20 vitest).

AC evidence (app/tests/test_router.py unless noted):
#1 test_queued_runs_in_order_and_refuses_when_full, test_single_refuses_while_one_runs, test_restart_cancels_the_running_invocation, test_parallel_runs_both, test_a_job_that_ends_during_submit_keeps_the_lane.
#2 test_assist_conversations_have_their_own_queues.
#3 test_changes_take_effect_only_between_invocations (definition and rule set), test_agent_turned_off_while_queued_does_not_run.
#4 test_fallback_modes_for_each_cause (5 causes x 4 modes, history reason), test_last_result_without_an_earlier_result_is_an_error, test_a_turn_that_cannot_start_returns_the_fallback, test_chat_retry_gives_up_and_returns_the_fallback.
#5 test_crash_resumes_queued_and_fails_running, test_stop_fails_running_and_keeps_queued.
#6 test_every_invocation_records_path_result_and_versions, test_cost_links_provider_calls_and_chat_retries (real Meter: 2 error rows + 1 ok row linked by invocation_id, cost 0.0012 USD).
Test endpoint: app/tests/test_router_test_api.py (5 tests, FakeHA receives no call_service).
<!-- SECTION:NOTES:END -->

## Comments

<!-- COMMENTS:BEGIN -->
author: @claude
created: 2026-11-15 00:58
---
Follow-up proposals (not created): (1) task-13 can reuse router.retry.RetryPolicy and retryable() for decision-call retries (design 18). (2) The Inspect screens (task-19) should show caller.draft=true test invocations as drafts, because agent_version is only the base version. (3) A writes field for the fixed fallback, if wanted, needs a definition change.
---
<!-- COMMENTS:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Added the router package: one submit/invoke/cancel entry point with source metadata, lanes per agent and per Assist conversation, the four HA concurrency modes, a rule-stage seam for task-13, the fallback for each cause with reasons, and a persistent queue in the invocations table that survives restarts. Provider calls now carry the invocation ID, so each result reports its cost. Chat requests retry with backoff around the meter. POST api/agents/{id}/test runs a dry-run turn (task-18 AC #5). Verified with 31 new tests and mise run check (435 pytest, frontend checks).
<!-- SECTION:FINAL_SUMMARY:END -->
