---
id: TASK-12
title: 'Router, concurrency queue and fallback'
status: In Progress
assignee:
  - '@claude'
created_date: '2026-11-14 23:04'
updated_date: '2026-11-15 00:42'
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
- [ ] #1 queued, single, restart and parallel modes behave as in section 9.6, with tests for each
- [ ] #2 Assist conversations use their own queue per conversation
- [ ] #3 Rule and definition changes take effect only between invocations
- [ ] #4 The fallback (last_result, fixed, no_change, error) is returned for each cause in section 9.7, and history records the reason
- [ ] #5 Queued invocations survive an app restart; running ones are recorded as failed and return the fallback
- [ ] #6 Every invocation records path, result, cost, definition version and rule version
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
