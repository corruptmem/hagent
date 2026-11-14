---
id: TASK-7
title: 'Agent definitions, versions and management API'
status: To Do
assignee: []
created_date: '2026-11-14 23:03'
labels:
  - app
dependencies:
  - TASK-6
references:
  - docs/design.md
priority: high
type: feature
ordinal: 7000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Store agent definitions with immutable versions, create the private knowledge store with each agent, and expose a JSON management API (plus change-event WebSocket) for the UI. Define the management API contract as part of this task.

Design: docs/design.md sections 7, 7.1, 10.6 and 22 (Management UI stack).
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Create, edit, copy, turn off and delete agents through the API, with validation of every definition field in section 7.1
- [ ] #2 Each change creates a new definition version; earlier versions can be listed, compared and restored
- [ ] #3 Deleting an agent asks whether to keep or delete its private store
- [ ] #4 No agent tool can write a definition (tested)
- [ ] #5 The API contract is documented
<!-- AC:END -->
