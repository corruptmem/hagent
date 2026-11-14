---
id: TASK-2
title: 'Spike: detect own writes through HA context IDs'
status: To Do
assignee: []
created_date: '2026-11-14 23:03'
labels:
  - spike
  - app
dependencies: []
references:
  - docs/design.md
priority: high
type: spike
ordinal: 2000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
The design assumes the app can link each service call it makes to the state changes that the call causes, through HA context IDs, so that subscriptions can ignore an agent's own writes and history can mark changes as agent or external.

Design: docs/design.md section 12 (Own writes).
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Confirmed against HA source or a live test whether the WebSocket call_service command returns or accepts a context
- [ ] #2 A test shows a state_changed event that can be matched to the call that caused it
- [ ] #3 If context matching is not possible, an alternative is documented with its limits
- [ ] #4 docs/design.md section 12 matches the verified behaviour
<!-- AC:END -->
