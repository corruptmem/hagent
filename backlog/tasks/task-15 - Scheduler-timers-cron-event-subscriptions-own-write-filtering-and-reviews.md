---
id: TASK-15
title: 'Scheduler: timers, cron, event subscriptions, own-write filtering and reviews'
status: In Progress
assignee:
  - '@claude'
created_date: '2026-11-14 23:04'
updated_date: '2026-11-15 01:23'
labels:
  - app
dependencies:
  - TASK-2
  - TASK-12
references:
  - docs/design.md
priority: high
type: feature
ordinal: 15000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Let agents schedule their own invocations with APScheduler (SQLite job store) and subscribe to HA events and state changes, with limits, debounce, own-write filtering through HA context IDs, the review schedule, and the missed-run policy.

Design: docs/design.md sections 12 and 14.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Timers, cron schedules and subscriptions persist and are restored on app start
- [x] #2 Minimum interval, maximum count and debounce limits are enforced
- [x] #3 Subscriptions ignore state changes that the agent caused itself, by default
- [x] #4 History marks each state change as agent or external
- [x] #5 Missed schedules run once on start if less than 10 minutes late, otherwise are skipped and recorded
- [x] #6 Review invocations run on the definition's schedule
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Add APScheduler==3.11.3 and SQLAlchemy (pinned). 4.x is still alpha (4.0.0a6, April 2025). Job store: SQLAlchemyJobStore in its own SQLite file in the data directory.
2. Migration 0800: schedules, subscriptions, schedule_runs (fires and skips), subscription_events (classified state changes with origin). These tables are the source of truth; the job store is reconciled on start.
3. scheduler/ package: SchedulerComponent (after router), SchedulerStore (AgentStateStore for schedule and subscription kinds; rules not_supported with a by-kind seam for task-13), strict ScheduleSpec (timer, cron, review flag) in SPEC_CHECKS, limits (max_schedules in the create transaction, min_interval for timers, cron gaps and at fire time, debounce floor from the definition).
4. Missed-run policy in start with a clock seam: less than 600 s late runs once, else skip and record. Review jobs per agent from definition.review, synced on agents events.
5. Subscriptions: HA event handler queues events; OwnWriteTracker with a small call observer seam on ToolsComponent and CallService.run; delivery re-checks agent and current scope; own writes ignored by default; payload redaction of approval action IDs and tags.
6. Tests with FakeHA and fake clock; update test_http_server list, executor CASES, README and docs.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Library: APScheduler 3.11.3 (pinned) with SQLAlchemy 2.1.8 and tzlocal 5.4.5. APScheduler 4 is still a pre-release (4.0.0a6, April 2025) with an unstable interface. APScheduler ships no type information. The root pyproject.toml has a mypy override that ignores its imports.

Job store: the SQLAlchemy job store uses its own SQLite file (scheduler.sqlite in the data directory). This keeps it out of the migrated schema and away from the write lock of the app database. The table scheduler_items (migration 0800) is the source of truth. On each start, the component makes the job store match it and the review cron of each definition.

Missed runs: APScheduler reads the wall clock directly, so a test cannot replace its clock. The component starts APScheduler paused, applies the policy with its own clock seam, then resumes it. The last missed due time decides: less than 600 s late runs once (missed_count gives the number of missed due times); 600 s or more is skipped and recorded as skipped_missed. A date job that APScheduler misses at runtime is also recorded, and the timer row is deleted.

Cron: APScheduler 3 reads day of week 0 as Monday. cron.py converts the standard numbers (0 and 7 = Sunday) to day names. A test checks this. A schedule spec cannot set both day of month and day of week. The definition review cron can (task-7 model); APScheduler then fires only when both match.

AC 4 scope: the app marks the origin (agent or external) of each state change that matches a subscription. The evidence is scheduler_events.origin and origin_agent_id, and changes[].origin and own in the delivered data. The app does not record state changes that no subscription watches.

Decisions:
- Schedules and subscriptions keep firing after the user turns off the self tools.
- A turned-off agent keeps its review job. Each due time records a refused row (agent_off).
- min_interval applies to agent schedules (timer lead time, smallest cron gap, and at fire time against the last submitted schedule run). The definition review does not count.
- The definition debounce is a floor for each subscription. The first match starts the period; all matches of the period go into one invocation.
- A write counts as an own write only for the agent that made the call.

Cross-area edits for review:
- tools/base.py: ServiceCallObserver protocol and ToolEnv.calls.
- tools/component.py: ToolsComponent.calls.
- tools/ha_tools.py: CallService.run calls call_started before the call and call_finished in finally.
- tools/store_tools.py: TimerScheduleSpec, CronScheduleSpec and _schedule_spec in SPEC_CHECKS (replaces not_supported).
- tools/__init__.py: export ServiceCallObserver.
- app.py: SchedulerComponent after router; tools.agent_state and tools.calls.
- tests/test_http_server.py: component list. tests/test_tool_executor.py: schedule CASES (invalid_spec, min_interval, not_owner).
- pyproject.toml (root): mypy override for apscheduler.

Validation: mise run check passes (ruff, mypy strict, 656 pytest tests, frontend checks).
<!-- SECTION:NOTES:END -->

## Comments

<!-- COMMENTS:BEGIN -->
author: @claude
created: 2026-11-15 01:23
---
Proposal for task-13 (not created): rule writes must go through ToolsComponent.calls or the tool executor. Otherwise an agent's own rule outputs count as external, and its own subscriptions start it again. Task-13 sets scheduler.state_store.rules to its rule store; until then rule calls give not_supported (list, create, update) or not_owner (set_enabled, delete).
---
<!-- COMMENTS:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Added the scheduler package (task-15). APScheduler 3.11.3 runs timers, cron schedules and the definition review schedule, with a SQLAlchemy job store in its own SQLite file. scheduler_items (migration 0800) is the source of truth; scheduler_runs records each run and skip; scheduler_events records each matched event with its origin. Strict schedule specs replace the not_supported seam. The store enforces max_schedules (in one transaction), min_interval (timer lead time, cron gap, fire time) and the debounce floor. Subscriptions re-check the agent and the current scope before each delivery, ignore own writes by default through OwnWriteTracker and a new ServiceCallObserver seam, and redact approval action IDs and tags.

Evidence (app/tests/test_scheduler.py, 40 tests): AC1 test_schedules_and_subscriptions_persist_across_a_restart; AC2 test_max_schedules_counts_schedules_and_subscriptions, test_min_interval_is_enforced_on_create, test_a_timer_at_a_time_too_soon_is_refused, test_min_interval_is_enforced_at_fire_time, test_debounce_merges_changes_and_the_definition_is_a_floor, test_a_longer_spec_debounce_wins; AC3 test_own_writes_are_ignored_and_history_marks_the_origin, test_include_own_writes_delivers_them_marked_as_agent, test_a_write_by_another_agent_is_not_an_own_write; AC4 origin and origin_agent_id assertions in the same tests (scope: state changes that match a subscription); AC5 test_missed_timer_runs_once_if_less_than_ten_minutes_late (3 min, 599 s, 600 s, 20 min), test_missed_cron_runs_coalesce_into_one_run, test_a_missed_cron_run_too_late_is_skipped_and_recorded; AC6 test_review_invocations_follow_the_definition_schedule, test_a_definition_change_moves_or_removes_the_review_job. Redaction: test_no_delivered_payload_holds_an_approval_secret. mise run check passes.
<!-- SECTION:FINAL_SUMMARY:END -->
