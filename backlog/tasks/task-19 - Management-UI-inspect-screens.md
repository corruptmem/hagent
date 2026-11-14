---
id: TASK-19
title: 'Management UI: inspect screens'
status: To Do
assignee: []
created_date: '2026-11-14 23:04'
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
