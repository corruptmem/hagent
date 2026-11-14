---
id: TASK-16
title: Approval flow for tool calls
status: To Do
assignee: []
created_date: '2026-11-14 23:04'
labels:
  - app
  - security
dependencies:
  - TASK-4
  - TASK-8
references:
  - docs/design.md
priority: medium
type: feature
ordinal: 16000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Send approval requests for tools or scopes marked for approval to the approvers named in settings, wait for the answer up to a time limit, and show pending requests in the management UI.

Design: docs/design.md sections 7.2 and 8 (Approval).
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Only approvers named in settings can approve or reject
- [ ] #2 An agent turn waits for the answer up to the time limit, then treats it as rejected
- [ ] #3 Pending approvals are visible and answerable through the management API
<!-- AC:END -->
