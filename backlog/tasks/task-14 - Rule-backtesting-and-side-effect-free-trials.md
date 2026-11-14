---
id: TASK-14
title: Rule backtesting and side-effect-free trials
status: To Do
assignee: []
created_date: '2026-11-14 23:04'
labels:
  - app
  - rules
dependencies:
  - TASK-13
references:
  - docs/design.md
priority: high
type: feature
ordinal: 14000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Let agents test rules before and after activation: backtest a draft rule version against recorded history with its agreement template, and run sampled trials whose agent turns write nothing.

Design: docs/design.md sections 10.1 (Agreement, Trial) and 10.7.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 A backtest replays matching past invocations (last 7 days by default) without writes, and stores the agreement rate and cost
- [ ] #2 Decision-rule backtests can be limited to a number of invocations
- [ ] #3 Trial agent turns write nothing, count towards the budget and stop when it is spent
- [ ] #4 History keeps inputs and context for the retention period so that backtests can run
<!-- AC:END -->
