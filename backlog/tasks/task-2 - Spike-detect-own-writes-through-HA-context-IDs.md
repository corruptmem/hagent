---
id: TASK-2
title: 'Spike: detect own writes through HA context IDs'
status: In Progress
assignee:
  - '@claude'
created_date: '2026-11-14 23:03'
updated_date: '2026-11-14 23:19'
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
- [x] #1 Confirmed against HA source or a live test whether the WebSocket call_service command returns or accepts a context
- [x] #2 A test shows a state_changed event that can be matched to the call that caused it
- [x] #3 If context matching is not possible, an alternative is documented with its limits
- [x] #4 docs/design.md section 12 matches the verified behaviour
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Read HA Core websocket_api/commands.py (handle_call_service) and core context code. Record whether call_service accepts and returns a context.
2. Write a live probe under spikes/task-2/: create a temporary input_boolean helper over WebSocket, subscribe to state_changed, toggle it with call_service, record the call result context and the event contexts, then delete the helper.
3. Write the matching rule as a small pure function. Add a unit test under app/tests if task-6/15 can reuse it; else keep it in the spike folder.
4. Update docs/design.md section 12 (Own writes) to match the verified behaviour.
5. Run mise run check, record notes, commit.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Source check (HA Core dev, commit fa470750): websocket_api/commands.py handle_call_service builds the context with connection.context(msg), which returns Context(user_id=self.user.id) and ignores the message. The command schema extends BASE_COMMAND_MESSAGE_SCHEMA, which does not allow extra keys. The result is {"context": context} plus "response" when return_response is true. The call runs with blocking=True.

Live probe (HA Core 2026.11.1, spikes/task-2/probe.py, report in spikes/task-2/result.json):
- call_service returned a context in the result for turn_on, toggle and a no-op turn_on.
- The state_changed event context ID and new_state context ID equalled the result context ID.
- HA sent the event before the result (seq 15 before 16, 61 before 62). A matcher must hold events while a call is in flight.
- The no-op turn_on returned a context and caused no event. Context IDs need a TTL.
- call_service with an extra context field failed with invalid_format. The client cannot supply a context.
- A toggle from a second connection had a new context ID that matched no own call.
- parent_id was null for all direct calls. user_id was set (same user for both connections).
- The probe created one temporary input_boolean helper with a random name, touched no other entity, and deleted the helper. input_boolean/list confirmed the deletion (deleted: true).

Built app/src/hagent_app/own_writes.py: OwnWriteTracker, a pure matcher with hold and TTL, for task-6 and task-15. app/tests/test_own_writes.py covers event-before-result, result-before-event, several events per call, external changes, hold expiry, arrival order, no-op TTL expiry, failed calls, and a replay of the live sequence.

Updated docs/design.md section 12 (Own writes): HA creates the context, the runtime learns it from the result, events arrive first, plus limits (parent_id indirect effects not followed, user_id not used, hold trade-off).

Not tested: indirect effects through automations (the brief allowed no automation). The parent_id behaviour comes from HA documentation and source, not from a live run.

AC3: context matching works (live probe and tests), so no alternative is needed. Section 12 still documents the limits of the match. mise run check passed: ruff, mypy strict, 11 tests.

Correction: the probe redacted user_id on both connections, so it did not compare them. The earlier note 'same user for both connections' is not proven by the probe; only 'user_id was set' is.

Extra source check (HA Core commit fa470750): helpers/entity.py sets CONTEXT_RECENT_TIME_SECONDS = 5. An entity keeps a call's context for 5 seconds. A device that reports its state later gets a new context, so the tracker marks it external. components/automation/entity.py creates the trigger context as Context(parent_id=<triggering context id>). Section 12 now scopes the live result to input_boolean and lists both limits.

Proposed follow-up (not created): a live check with a real device integration and with an automation, to confirm late-report contexts and parent_id.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Verified that HA creates the call_service context and returns it in the result. The client cannot supply one (invalid_format, live and source). Live probe on HA 2026.11.1 showed that the state_changed event carries the same context ID but arrives before the result. Added OwnWriteTracker (pure, hold + TTL) with 10 unit tests including a replay of the live sequence. Updated design section 12 with the verified behaviour and its limits. The temporary helper was deleted and the deletion confirmed. Evidence: spikes/task-2/result.json, mise run check.
<!-- SECTION:FINAL_SUMMARY:END -->
