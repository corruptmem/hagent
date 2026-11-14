---
id: TASK-8
title: Tool executor with scope enforcement and audit log
status: To Do
assignee: []
created_date: '2026-11-14 23:03'
labels:
  - app
  - security
dependencies:
  - TASK-6
  - TASK-7
references:
  - docs/design.md
priority: high
type: feature
ordinal: 8000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Build the tool executor and the scope check that every tool call passes, for the ha, knowledge, memory and self tool groups. The app has admin access to HA, so this check is the only barrier between an agent and full access.

Design: docs/design.md sections 8 and 19.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 read and control scopes resolve area, label, device and floor targets to entity IDs before the check
- [ ] #2 Entity IDs inside service data are checked, not only targets
- [ ] #3 Indirect services (script, automation, scene, generic homeassistant, shell_command, rest_command, python_script, pyscript, hassio, notify) are denied unless the scope names them
- [ ] #4 admin is a blanket scope with no narrower form
- [ ] #5 Scopes are re-resolved when HA registries change, and a growing scope sends an HA notification
- [ ] #6 Every call that changes HA is written to the audit log; admin changes keep the previous version
- [ ] #7 For every tool, a test proves that an out-of-scope call never reaches HA or the store
<!-- AC:END -->
