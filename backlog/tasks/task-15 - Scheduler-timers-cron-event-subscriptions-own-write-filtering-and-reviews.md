---
id: TASK-15
title: 'Scheduler: timers, cron, event subscriptions, own-write filtering and reviews'
status: To Do
assignee: []
created_date: '2026-11-14 23:04'
labels:
  - app
dependencies:
  - TASK-2
  - TASK-12
references:
  - docs/design.md
priority: high
type: feature
ordinal: 15000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Let agents schedule their own invocations with APScheduler (SQLite job store) and subscribe to HA events and state changes, with limits, debounce, own-write filtering through HA context IDs, the review schedule, and the missed-run policy.

Design: docs/design.md sections 12 and 14.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Timers, cron schedules and subscriptions persist and are restored on app start
- [ ] #2 Minimum interval, maximum count and debounce limits are enforced
- [ ] #3 Subscriptions ignore state changes that the agent caused itself, by default
- [ ] #4 History marks each state change as agent or external
- [ ] #5 Missed schedules run once on start if less than 10 minutes late, otherwise are skipped and recorded
- [ ] #6 Review invocations run on the definition's schedule
<!-- AC:END -->
