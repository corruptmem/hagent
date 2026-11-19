---
id: TASK-4
title: 'Spike: deliver approval requests as actionable HA notifications'
status: Done
assignee:
  - '@claude'
created_date: '2026-11-14 23:03'
updated_date: '2026-11-14 23:27'
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
- [x] #1 A working example sends an actionable notification to the HA mobile app and receives accept or reject in the app
- [x] #2 The mapping from an approver (HA user) to a notify service is defined
- [x] #3 Behaviour when no response arrives before the time limit is defined
- [x] #4 Findings are recorded in the task notes
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. List notify services and mobile_app devices over the HA REST API (read only).
2. Ask the lead which notify target to use. Keep candidate names out of files.
3. Write a probe script under spikes/task-4/. It sends one actionable notification with accept and reject actions and a unique action ID. It listens on the WebSocket API for the mobile_app_notification_action event and filters on that ID.
4. Run the probe live. Tell the lead when the notification is sent. Wait up to 10 minutes for the tap.
5. Read HA docs and source for person, auth user, mobile_app config entries and device trackers. Define the approver to notify-target mapping and the timeout behaviour.
6. Record findings in the task notes. Run mise run check. Commit.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
## Findings (live probe, 2026-11-14)

Terms:
- Registration: one mobile_app config entry. The HA Companion app creates it when a person signs in on a phone. It holds a webhook ID, a device ID, a device name and the ID of the HA user who signed in.
- Legacy notify service: a service such as notify.mobile_app_<device>. HA derives its name from the device name of the registration.
- Action event: the mobile_app_notification_action event. The app fires it when a person taps an action button.

Probe: spikes/task-4/actionable_notify.py. It subscribes to the action event over the WebSocket API. It then sends one notification with an accept action and a reject action. Each action ID contains a random request ID. The notification tag also contains it. On timeout, the probe sends clear_notification with the same tag.

Live run 1:
- The call to notify.mobile_app_<device> succeeded. HA logged no push error and no rate-limit warning.
- The payload was correct: message set, actions under data.actions.
- The notification did not arrive on the phone that the person uses.
- Cause: the HA user of that person has two registrations with the same device name. One is the active phone. The other is a second phone that does not report today.
- The legacy notify platform keys its targets by device name (mobile_app notify.py, push_registrations). The last registration that loads wins. The service therefore routed to the second phone. HA gave no error.
- Evidence: the notify entity of the second registration recorded a send at the exact send time. The notify entity of the active registration did not change.
- The notify entities (notify.send_message) reach each registration separately. They accept only message and title, so they cannot carry actions.

## Mapping: approver to notify target (AC #2)

Rule: the mapping must target a mobile_app registration (device ID and webhook ID), not a device-name service. Duplicate device names silently route to the wrong phone.

Source facts (HA Core, mobile_app):
- http_api.py stores the ID of the signed-in HA user in the registration data (user_id).
- webhook.py fires the action event with context.user_id set to that user ID (registration_context).
- notify.py: the base legacy service accepts target as a list of webhook IDs. A per-device service ignores target.

Proposed mapping:
1. Settings store approvers as HA user IDs (design 7.2).
2. The hagent integration runs in HA Core. It resolves each approver to the loaded mobile_app registrations whose user_id equals the approver ID.
3. The integration sends the notification to those webhook IDs. Webhook IDs are secrets: anyone who knows one can post to it without a token. They must not leave HA Core, so the app asks the integration to send, and does not resolve webhook IDs itself.
4. Optional explicit config: the user can pick a subset of the registrations of each approver (by device ID) in Settings. Settings show the resolved devices for each approver and offer a test notification.
5. person entities and device trackers are not a reliable source. One person in the test home links two mobile_app trackers. One person has no HA user.
6. Unproven: send through the base service with target set to a webhook ID. The probe could not test this, because webhook IDs are not visible over the API. The first implementation task must prove it, or call the push path in another way.

## Authorisation of the answer

- Accept an action event only if context.user_id is one of the approvers. Any HA user with API access can fire the same event type by hand.
- Use an unguessable request ID in the action ID and the tag. Ignore events for unknown request IDs.
- Use authenticationRequired on iOS actions, so the phone must be unlocked to approve.

## Behaviour without an answer (AC #3)

- HA gives no time limit and no delivery receipt for a notification. The runtime must own the time limit.
- On timeout the request counts as rejected (fail closed). The tool call returns an approval_timeout error to the agent turn.
- On timeout the runtime sends clear_notification with the request tag to every approver registration.
- The first valid answer wins. The runtime then clears the notification on the other registrations.
- A late answer for an expired request does not change anything. The runtime logs it in the audit log.
- If the send fails, or an approver has no registration, the request fails closed at once.
- If the app restarts, pending requests expire as rejected.
- Inspect > Approvals shows pending, answered and expired requests (design 7.2).

## Live runs 2 to 4 (2026-11-14)

- The person removed the stale second registration in HA. The legacy notify service then routed to the active registration. Evidence: the notify entity of the active registration recorded each later send at the exact send time.
- Run 2: the person tapped the notification body, not an action button. The notification closed. No mobile_app_notification_action event arrived. Result: a body tap is neither accept nor reject. It counts as no response, and the request stays pending until the time limit.
- Run 3 (success, AC #1 evidence): the probe sent at 23:26:13 UTC. The person tapped Accept. The probe log shows:
  - ANSWER decision=ACCEPT after=8.8s
  - event data keys=['action']
  - event origin=REMOTE, context.user_id set=True
  - context user matches approver person=True
- The iOS app fired a second event at the same time: ios.notification_action_fired, with no 'action' key value. The runtime must listen only to mobile_app_notification_action and ignore the legacy event type, or it can count one tap twice.
- The match of context.user_id to the approver confirms the source fact: the app fires the event in the context of the HA user of the registration. The runtime can authorise the answer with this user ID.
- Run 1 (stale registration) left a notification on the removed phone. The probe stopped before its clear step. This has no effect, because the registration no longer exists.

Lead: open item (send via base notify service with target=[webhook_id] inside HA Core) is passed to task-16/task-17.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Built a probe (spikes/task-4/actionable_notify.py). It sends an actionable notification with accept and reject actions. It receives the answer over the WebSocket API. Live run 3 proved the full loop: the person tapped Accept, and the probe received mobile_app_notification_action after 8.8 s. The event context user matched the approver. Run 1 found a hazard: two registrations with the same device name route the legacy notify service to the wrong phone, and HA reports no error. The approver mapping therefore targets mobile_app registrations by HA user ID, not device-name services. Timeout, body-tap and late-answer behaviour fail closed. Details are in the notes. Open point: send through the base service with a webhook ID target is not proven yet. mise run check passes.
<!-- SECTION:FINAL_SUMMARY:END -->
