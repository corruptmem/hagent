---
id: TASK-17
title: >-
  HA integration: app link, agent devices, hagent.invoke, AI Task and
  conversation entities
status: To Do
assignee: []
created_date: '2026-11-14 23:04'
labels:
  - integration
dependencies:
  - TASK-7
  - TASK-12
references:
  - docs/design.md
priority: high
type: feature
ordinal: 17000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Build the thin custom integration: the authenticated WebSocket link to the app, one device per agent with its entities, the hagent connectivity sensor, the hagent.invoke action, the AI Task entity and the optional conversation entity. Define the link protocol as part of this task. The integration must stay compatible with HA Core's Python 3.14.

Design: docs/design.md sections 6, 9.2, 9.3 and 15.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 The link authenticates with the app's shared secret and recovers from disconnects
- [ ] #2 Agent devices and entities appear, update and disappear as agents change in the app
- [ ] #3 hagent.invoke supports data, wait, timeout and on_timeout, and returns the response data in section 9.2
- [ ] #4 The AI Task entity supports GENERATE_DATA, SUPPORT_ATTACHMENTS and EVALUATE
- [ ] #5 The conversation entity exists only for agents that allow Assist
- [ ] #6 When the app is unreachable, calls fail with an error and the connectivity sensor turns off
- [ ] #7 The link protocol is documented
<!-- AC:END -->
