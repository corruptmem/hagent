---
id: TASK-13
title: 'Rule engine: sandboxed HA templates, decision actions, context and cache'
status: To Do
assignee: []
created_date: '2026-11-14 23:04'
labels:
  - app
  - rules
dependencies:
  - TASK-12
references:
  - docs/design.md
priority: high
type: feature
ordinal: 13000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Build rules: state, order, match, context, action (template or decision), result, writes, cache and agreement, with immutable versions and statistics per version. Render all rule templates in the app with a sandboxed Jinja environment that supports a documented subset of HA template functions, against the state cache filtered to the agent's scope. Define the supported template subset as part of this task.

Design: docs/design.md sections 10.1 to 10.6.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Templates cannot read entities outside the agent's scope; states() returns unknown for them (tested)
- [ ] #2 The supported HA template subset is documented and tested against HA's own output for common templates
- [ ] #3 Decision actions send one request with one or more questions, using inputs, caller data and context
- [ ] #4 Context items (history, action_response, store_file, invocation_history) respect scope and cache times
- [ ] #5 Rules cannot write outputs that need approval
- [ ] #6 Every rule change creates a new version with its own statistics; rollback works
- [ ] #7 Template rules work while HA Core restarts and while the internet is down
<!-- AC:END -->
