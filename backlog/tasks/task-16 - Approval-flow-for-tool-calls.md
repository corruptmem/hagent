---
id: TASK-16
title: Approval flow for tool calls
status: Done
assignee:
  - '@claude'
created_date: '2026-11-14 23:04'
updated_date: '2026-11-15 00:56'
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
- [x] #1 Only approvers named in settings can approve or reject
- [x] #2 An agent turn waits for the answer up to the time limit, then treats it as rejected
- [x] #3 Pending approvals are visible and answerable through the management API
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Add package approvals/: ApprovalsComponent (before tools in default_components), its gate for ToolsComponent.approval, a Notifier protocol, MobileAppNotifier, and API routes.
2. Settings: option section approvals with approvers (HA user IDs), timeout_seconds and notify_services (per-approver override, approved by the lead). Task-20 can replace the source. No approvers: fail closed.
3. Migration 0900: approval_requests and approval_answers.
4. Executor: the gate can return ApprovalOutcome with approval_rejected, approval_timeout or approval_unavailable. Only True or an accepted outcome runs the call.
5. Gate: 128-bit request ID, strict action-ID parse, one conditional UPDATE decides each request, time limit counts from creation, clear_notification on every target after the end, cancel marks the row cancelled, start expires pending rows, stop wakes waiters.
6. Answer check: only mobile_app_notification_action; context.user_id must be an approver of the request and of the settings, never None, never the app's own HA user (auth/current_user). API answers use X-Remote-User-Id.
7. Notifier: override, else person user_id -> mobile_app device trackers -> device -> notify.mobile_app_<slug>; send only if the service exists and exactly one mobile_app device has the slug.
8. API: GET /api/approvals, GET /api/approvals/{id}, POST /api/approvals/{id}/answer; change-event topic approvals. docs/api.md and app/README.md.
9. Tests with FakeHA. mise run check. Commit.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
## Implementation (2026-11-15)

Terms: approval request = one held tool call. Action event = mobile_app_notification_action. Registration = one mobile_app config entry.

Built:
- app/src/hagent_app/approvals/: config.py (option section approvals), notifier.py (Notifier, MobileAppNotifier), service.py (ApprovalService: gate, answers, records), api.py (routes), component.py (ApprovalsComponent, fail-closed gate).
- Migration 0900_approvals.sql: approval_requests and approval_answers. approval_answers records every answer for a known request with its outcome: applied, late, not_approver, no_user or app_user.
- Routes: GET api/approvals, GET api/approvals/{id}, POST api/approvals/{id}/answer. Change-event topic approvals. docs/api.md and app/README.md describe them.

Decisions:
- Approvers come from the approvals option section until the settings store exists (task-20). Keys: approvers, timeout_seconds (default 300, max 3600), notify_services.
- Delivery: the app cannot read registration data (user_id, webhook_id) over the WebSocket API. The lead approved the person chain: person entity with user_id = approver -> its device_trackers of platform mobile_app -> device -> notify.mobile_app_<slug>. The lead also asked for a per-approver notify_services override. It wins over the chain.
- Uniqueness: the notifier sends to a service only if the service exists and exactly one mobile_app device has its slug. Otherwise it records the problem and does not send. Slugs use python-slugify, the library of HA util.slugify (new runtime dependency, with text-unidecode).
- If no notification reaches an approver, the request fails at once (approval_unavailable).
- The notification text names only the agent, the tool, and the checked services and entities. It never contains free-form service data.
- API answers take the user from X-Remote-User-Id. Supervisor source check: api/ingress.py removes client copies of the X-Remote-User-* headers and sets them after the copy. IngressGuard admits only the Supervisor address by default.

Cross-area edits (keep in mind for merges):
- tools/executor.py and tools/__init__.py: ApprovalOutcome and _approval_outcome. A gate exception now maps to approval_unavailable. This overlaps task-8-fix (catch-all in the same function).
- ha/client.py: subscribe_events no longer raises when the connection closes during the subscribe. The subscription stays registered and the client subscribes after the next connect. Cause: test_main failed in 2 of 5 runs. A component start (tools registry) crashed the app when HA dropped during subscribe. The new component changed start timing and exposed it. Base commit: 0 of 10 failures. After the fix: 25 of 25 repro runs and 5 of 5 test_main runs passed. New test: test_subscribe_during_a_drop_subscribes_after_reconnect.
- tests/helpers/fake_ha.py: FakeHA.user and auth/current_user; fire_event(context=...).
- tests: test_http_server.py (component list), test_ha_catalog.py (auth/current_user is a read command), test_api_doc.py ({request_id}).

Limits and assumptions for the security review:
- A user can link any device tracker to a person. A request can reach the phone of another person. That phone cannot approve: the check uses context.user_id. Test: test_wrong_phone_cannot_approve.
- The exclusion of the app's own HA user assumes that auth/current_user over the Supervisor proxy returns the user ID that HA puts in the context of the app's calls. FakeHA only simulates this. Confirm live when the app is packaged. It matters because an admin-scoped agent can see action IDs in call_service event data and could fire the action event through a script.
- With a non-default ingress.allowed_addresses, each client at those addresses can set X-Remote-User-Id. docs/api.md states this.
- A cancel during the send does not clear notifications that HA already accepted. A later answer counts as late, so it has no effect.
- A cancel after an accept leaves the row accepted, but the call never runs and has no tool_audit row. A fix needs an executor change; left for after task-8-fix.
- An internal error after an answer decided the row still returns approval_unavailable (fail closed).
- The notification for self.*, knowledge.* and memory.* tools names only the agent and the tool. The full request is in the API.
- No UI: the task description mentions the management UI, but AC #3 asks for the API only. No Inspect > Approvals screen exists yet.

Validation: mise run check passes (ruff, mypy strict, 429 pytest tests, frontend checks). app/tests/test_approvals.py: 49 tests.

AC evidence (uv run pytest app/tests/test_approvals.py: 49 passed):
- AC #1: test_answers_from_other_users_have_no_effect (other user, no user, app user), test_wrong_phone_cannot_approve, test_legacy_and_malformed_events_are_ignored, test_api_refuses_non_approvers (no header, other user, app user), test_held_call_reaches_ha_only_after_a_valid_accept.
- AC #2: test_timeout_fails_closed_and_clears (approval_timeout, no light.turn_on in FakeHA commands), test_time_limit_counts_from_the_request, test_late_answer_after_timeout_is_only_logged, test_cancelled_turn_clears_and_ignores_later_accept, test_restart_expires_pending_requests.
- AC #3: test_api_lists_and_answers_pending_requests (list with status=pending, read, accept through POST, call reaches HA after it, second answer 409), test_api_errors.

## Security review fixes (2026-11-15)

Merged impl into task-16 first (task-8-fix, task-10, task-11). Kept the task-8-fix executor behaviour: catch-all check_failed, states_unavailable, strict self.* specs. Conflicts in docs/api.md, app/README.md, app/pyproject.toml and app.py: both sides kept. uv.lock regenerated.

Fixes:
- M1: after an accept, the gate loads the agent again (AgentReader). It denies with approval_stale if the agent is deleted, off or has another version. The reason column records why. An agents change event ends that agent's pending requests with status cancelled, so the turn gets approval_stale at once. Remaining window: a change between the post-accept check and tool.run (one audit write) is not checked again.
- M2: the admin_change summary names the command type (only if it is in CHANGE_COMMANDS) and fixed ID keys (entity_id, device_id, area_id, floor_id, label_id, dashboard_id, url_path, <helper>_id) whose value matches [a-z0-9_.-]{1,100}.
- M3: tools/store_tools.py denies subscriptions to call_service at every level (DENIED_EVENT_TYPES). Task-15 can still add redaction.
- L1: the executor writes a denial row approval_cancelled under asyncio.shield when the invocation is cancelled during the gate, then raises again. The approval step is now ToolExecutor._approve.
- L2: the API trusts X-Remote-User-Id only from peer 172.30.32.2. Option approvals.dev_trust_user_header turns this off for development, with a warning at start.
- L3: a dry run returns before the approval hook for every tool that needs approval, also read tools.
- L4: an answer UPDATE also needs expires_at > now; otherwise the answer is late.

Regression tests (test_approvals.py, test_review_*): one per probe p1, p1b, p2, p3, p3b, p4, p5, p6, plus agent-change cancel, unrelated change, admin summary without free text, call_service at read/control/admin, dev option warning. test_tool_executor.py::test_admin_event_subscription_is_strict now uses automation_triggered. The 8 original probes all fail against the new code. mise run check: 580 passed, lint, mypy and frontend pass.

Lead: an independent review found 0 critical, 0 high, 3 medium and 4 low issues; all 7 fixed on task-16. The lead re-ran the 8 original probes against impl: all now fail (fixed). Remaining window (agent change between the post-accept check and tool.run) is accepted. Approvals UI screen goes to task-19.
<!-- SECTION:NOTES:END -->

## Comments

<!-- COMMENTS:BEGIN -->
author: @claude
created: 2026-11-15 00:41
---
Proposed follow-up (not created): an Inspect > Approvals screen in the management UI that lists pending, answered and expired requests and answers through POST api/approvals/{id}/answer. Also: live confirmation that auth/current_user via the Supervisor proxy matches the context user of app calls; and an audit row when a turn is cancelled after an accept.
---
<!-- COMMENTS:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Added the approval flow for tool calls. A new approvals component supplies the gate of the tool executor. It sends an actionable notification to the phones of the approvers, waits up to timeout_seconds from the request, and treats no answer as approval_timeout. Only approvers can answer: the action event context.user_id, or the Ingress X-Remote-User-Id header for the API. Answers from other users, no user and the app's own user have no effect and are recorded. One conditional UPDATE decides each request, so the first valid answer wins. A restart expires pending requests. Delivery uses the person chain or an explicit override, and fails closed on a missing service or a duplicate device name. Pending requests are listed and answerable through api/approvals. Verified with 49 FakeHA tests in test_approvals.py and mise run check (429 tests). No UI screen yet; limits are in the notes.
<!-- SECTION:FINAL_SUMMARY:END -->
