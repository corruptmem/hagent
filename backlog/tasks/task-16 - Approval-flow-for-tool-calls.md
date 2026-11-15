---
id: TASK-16
title: Approval flow for tool calls
status: In Progress
assignee:
  - '@claude'
created_date: '2026-11-14 23:04'
updated_date: '2026-11-15 00:23'
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

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Add package approvals/: ApprovalsComponent (before tools in default_components), an ApprovalGate for ToolsComponent.approval, a Notifier protocol, MobileAppNotifier, and API routes.
2. Settings: config section 'approvals' with approvers (HA user IDs) and timeout_seconds. Task-20 can replace the source later. No approvers means fail closed.
3. Migration 0900: approval_requests and approval_answers (every received answer, also late, wrong-user and refused answers).
4. Executor: let the gate return an outcome with a code (approval_rejected, approval_timeout, approval_unavailable). Keep the bool form.
5. Gate: 128-bit request ID, strict action-ID parse, conditional UPDATE decides the first answer, clear_notification on all reached registrations after any end, cancel marks the row cancelled, start expires pending rows, stop rejects waiters.
6. Answer check: only mobile_app_notification_action; context.user_id must be an approver of the request and of current settings, never None and never the app's own HA user. API answers use the Ingress X-Remote-User-Id header (Supervisor strips client copies).
7. Notifier: person user_id -> device_trackers -> mobile_app device -> notify.mobile_app_<slug>; send only if the slug is unique and the service exists, else record why. Text from services and entities only.
8. API: GET /api/approvals, GET /api/approvals/{id}, POST /api/approvals/{id}/answer; change events topic approvals. Update docs/api.md and app/README.md.
9. Tests with FakeHA: held call absent before accept; reject, timeout, wrong user, None user, app user, legacy event, bad action ID, races, late answer, cancel, restart, notifier failure and ambiguity, API auth cases.
10. mise run check, notes, commit.
<!-- SECTION:PLAN:END -->
