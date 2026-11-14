---
id: TASK-4
title: 'Spike: deliver approval requests as actionable HA notifications'
status: To Do
assignee: []
created_date: '2026-11-14 23:03'
labels:
  - spike
  - app
dependencies: []
references:
  - docs/design.md
priority: medium
type: spike
ordinal: 4000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Tool calls marked for approval send an HA notification with accept and reject actions to named approvers. Find how to map an HA user to a notify target and receive the action response reliably.

Design: docs/design.md section 8 (Approval) and 7.2 (Settings).
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 A working example sends an actionable notification to the HA mobile app and receives accept or reject in the app
- [ ] #2 The mapping from an approver (HA user) to a notify service is defined
- [ ] #3 Behaviour when no response arrives before the time limit is defined
- [ ] #4 Findings are recorded in the task notes
<!-- AC:END -->
