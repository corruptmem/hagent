---
id: TASK-6
title: >-
  App skeleton: configuration, SQLite storage, HA WebSocket client and state
  cache
status: To Do
assignee: []
created_date: '2026-11-14 23:03'
labels:
  - app
dependencies:
  - TASK-1
  - TASK-2
references:
  - docs/design.md
priority: high
type: feature
ordinal: 6000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Create the runnable foundation of the hagent app: configuration loading, SQLite storage with migrations, a WebSocket client that connects to HA Core through the Supervisor proxy with SUPERVISOR_TOKEN, and the HA state cache fed by state-change subscriptions. Define the SQLite schema as part of this task.

Design: docs/design.md sections 6, 7 and 13.2.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 The app starts, connects to HA through the Supervisor proxy and reconnects after an HA restart
- [ ] #2 The state cache holds current entity states and updates on state changes
- [ ] #3 SQLite schema migrations run on start and are tested
- [ ] #4 Tests run without a live HA instance, using a fake WebSocket server
<!-- AC:END -->
