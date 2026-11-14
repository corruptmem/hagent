---
id: TASK-12
title: 'Router, concurrency queue and fallback'
status: To Do
assignee: []
created_date: '2026-11-14 23:04'
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
