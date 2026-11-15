---
id: TASK-13
title: 'Rule engine: sandboxed HA templates, decision actions, context and cache'
status: In Progress
assignee:
  - '@claude'
created_date: '2026-11-14 23:04'
updated_date: '2026-11-15 01:38'
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
- [x] #1 Templates cannot read entities outside the agent's scope; states() returns unknown for them (tested)
- [x] #2 The supported HA template subset is documented and tested against HA's own output for common templates
- [x] #3 Decision actions send one request with one or more questions, using inputs, caller data and context
- [x] #4 Context items (history, action_response, store_file, invocation_history) respect scope and cache times
- [x] #5 Rules cannot write outputs that need approval
- [x] #6 Every rule change creates a new version with its own statistics; rollback works
- [x] #7 Template rules work while HA Core restarts and while the internet is down
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Add jinja2. New package app/src/hagent_app/rules/ (spec, templates, checks, context, stage, store, component, config).
2. rules/templates.py: ImmutableSandboxedEnvironment with a documented HA subset (docs/rule-templates.md). Scoped state view: read scope (ha none: inputs only; admin: all), built once per invocation from the state cache and the last registry snapshot. Static check of names, filters, tests and blocked statements. Size checks on every argument and output, time limit by trace function, outer wait limit on an own thread pool.
3. Migration 0700_rules.sql: rules, rule_versions (immutable), rule_stats per version.
4. rules/store.py: RuleStore (versions, draft/active/off, reorder, rollback, stats). AgentStateStores serves the kind rule; default_components sets scheduler.state_store.rules to it (task-15 seam). TurnHistory.rules replaces the rule_owner/rule_stats stubs.
5. rules/checks.py as SPEC_CHECKS['rule']: strict RuleSpec, template check, decision model, history read scope, action_response only for response-only services with check_service_call and no approval, store_file attachment, writes with check_service_call (placeholder data) and no approval.
6. rules/stage.py RuleEngine (router RuleStage): first matching active rule runs; cache (key includes definition version); context items through ToolExecutor.without_approval(); template or decision action (one request, retries, decline on failure); result template (decline); write pre-check then writes through ha.call_service on the same executor (scope, audit, ToolEnv.calls own-write observer); stats with cost per version.
7. Tool seams: ServiceInfo.response_only, ToolExecutor.without_approval() and .env, self.reorder_rules and self.rollback_rule with CASES entries.
8. AC2: record HA render_template output for 71 synthetic templates (record.py, read only, no real entity) and compare in tests. No homeassistant dev dependency.
9. Tests: test_rules.py, test_rule_templates.py, test_rule_templates_ha.py, test_rules_component.py, RULE_CASES in test_tool_executor.py. Docs: docs/rule-templates.md, app/README.md section Rules.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Implementation notes (task-13).

Edits outside the rules area:
- tools/registry.py: ServiceInfo.response_only, from the get_services field response.optional == false. Verified against a real HA 2026.11.1 (weather.get_forecasts, calendar.get_events).
- tools/executor.py: ToolExecutor.without_approval() and the env property. The rule stage uses an executor with no approval gate, so a call that needs approval is denied (approval_unavailable) with an audit row.
- tools/stores.py: AgentStateStore gets reorder_rules and rollback_rule. tools/store_tools.py: SPEC_CHECKS['rule'] (lazy import of rules.checks), self.reorder_rules, self.rollback_rule.
- scheduler/component.py: SchedulerStateStore forwards reorder_rules and rollback_rule to its rules store.
- turns/history.py: TurnHistory.rules replaces the rule_owner and rule_stats stubs.
- router/rules.py: docstrings only.
- Test helpers: home.py (input_number.flow, weather.home, input_number.set_value, weather.get_forecasts response-only), fake_ha.py (get_config with time_zone), tools.py (FakeAgentState gets the two new methods), test_ha_catalog.py READ_COMMANDS gets get_config (the rules component reads HA's time zone).

Decisions:
- AC2 evidence: HA render_template output for 71 synthetic templates, recorded once from HA 2026.11.1 with app/tests/fixtures/ha_templates/record.py (read only; state functions name only *.hagent_fixture_* entities that do not exist; no time-dependent template). The homeassistant package was not added as a dev dependency: it resolves only to homeassistant 2026.2.3 and downgrades aiohttp to 3.13.3 in the shared lock.
- The comparison found two real differences, now fixed: round() with precision 0 returns an int, and median keeps int inputs.
- Rule writes run through ha.call_service on the tool executor, so they use ToolEnv.calls (the scheduler's ServiceCallObserver) as task-15 asks. Test: test_rule_writes_reach_the_own_write_observer.
- action_response needs a service that only returns data, and the normal service call check. A read-level agent therefore cannot use it (check_service_call needs control or outputs).
- Templates for ha: none see only the input entities (design 10.2), not the outputs.

Known limits (also in docs):
- Partial writes: if write 1 succeeds and write 2 fails, the rule records rule_error and the agent turn runs; the turn can write again.
- The result cache and the context cache are in memory only; they are empty after an app start.
- Writes fail closed while HA restarts (states_unavailable -> rule_error).
- A match template that fails stops the rule stage (rule_error -> agent turn); later rules do not run for that invocation (design 18).
- RuleOutcome.rules_version is not set; the router does not record it.
- For task-14: the engine always runs with dry_run=False; backtests need a dry-run path.
- One slow C call (for example a backtracking regex) cannot be stopped; the wait ends after render_seconds + 1 and the thread runs on in a pool of 4.

Validation: mise run check passes (ruff, mypy strict, 891 pytest tests, frontend checks).

AC evidence (tests pass in mise run check):
- AC1: test_templates_cannot_read_entities_outside_the_scope, test_an_agent_with_no_ha_access_sees_only_its_inputs, test_state_functions_read_only_the_scoped_view.
- AC2: docs/rule-templates.md; test_the_sandbox_matches_ha (71 recorded HA 2026.11.1 cases, value and type).
- AC3: test_a_decision_rule_sends_one_request_with_all_questions, test_a_failing_decision_call_retries_then_declines.
- AC4: test_history_context_respects_scope_and_cache_time, test_a_cached_context_item_is_checked_against_the_scope, test_an_out_of_scope_history_item_never_reaches_ha, test_action_response_context_calls_a_data_action, test_store_file_context_needs_an_attached_store, test_invocation_history_reads_own_invocations_only, RULE_CASES in test_tool_executor.py.
- AC5: RULE_CASES approval_required (create time), test_a_write_that_needs_approval_never_reaches_ha, test_the_rule_executor_denies_approval_calls_without_a_gate (fire time).
- AC6: test_each_change_creates_a_version_with_its_own_statistics, test_rollback_to_an_unknown_version_fails.
- AC7: test_template_rules_work_while_ha_restarts_and_offline, test_the_router_answers_with_a_rule_while_ha_restarts, test_an_area_scope_uses_the_last_registry_while_ha_restarts.
<!-- SECTION:NOTES:END -->

## Comments

<!-- COMMENTS:BEGIN -->
author: @claude
created: 2026-11-15 01:38
---
Proposed follow-ups (not created): (1) management API routes to list rules with versions and statistics and to roll back (design 10.6, for task-19/task-20). (2) A template check route, so that the UI can show the error for a function outside the subset (design 10.2); rules.templates.check_template is ready for it. (3) Persistent rule result and context caches. (4) task-14: a dry-run path in RuleEngine for backtests and trials; agreement and trial specs are stored already. (5) Record rules_version in invocations if the UI needs it.
---
<!-- COMMENTS:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Built the rule engine (design 10.1-10.6): package rules/ with a strict RuleSpec, a sandboxed Jinja engine with a documented HA subset (docs/rule-templates.md) that reads a state view filtered to the agent's scope, context items (history, action_response, store_file, invocation_history) with cache times, template and decision actions (one decision request with all questions), result templates with decline, writes through the tool executor (scope check, audit row, own-write observer, no approval gate), a result cache, immutable versions with statistics per version, rollback and reorder (migration 0700). The rule spec check is SPEC_CHECKS['rule']; the engine is the router's rule stage; the store serves the rule kind of the scheduler state store. Verified with mise run check (891 tests): scope tests for templates and every context item and write, approval denial at create and fire time, versions and rollback, cache, decision request shape, HA-restart and offline behaviour at engine and router level, sandbox limit probes, and 71 cases compared with HA 2026.11.1 render_template output.
<!-- SECTION:FINAL_SUMMARY:END -->
