---
id: TASK-13
title: 'Rule engine: sandboxed HA templates, decision actions, context and cache'
status: Done
assignee:
  - '@claude'
created_date: '2026-11-14 23:04'
updated_date: '2026-11-15 02:23'
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

Security review fixes (task-13-fix):
10. Render rule templates in a separate worker process (rules/render_worker.py, pool in rules/render_pool.py). The worker starts with an empty environment (TZ only), cwd /, python -I. It sets RLIMIT_AS after its imports and a soft RLIMIT_CPU before each render. The parent kills and replaces a worker that passes the wall-clock limit. The parent sends plain data only (scoped state snapshot, time zone key, variables). The worker answers with JSON, not pickle. This closes H1, H2, H3, M1 and L1 as a hard limit.
11. rules/__init__.py imports lazily, so that the worker does not load the app (about 120 MB and 0.9 s per worker otherwise).
12. In-process first line: _Deadline is a BaseException and a removed tracer fails the render (H1). deep_size walks Namespace, dict views and TemplateState (H2). A counting concat stops the output at MAX_OUTPUT (H3). The format check rejects * width and precision (M2). int * int checks the bit length (L1).
13. L2: the result cache key holds a digest of the visible entity IDs. check_writes runs before the cache lookup.
14. Regression tests from the review probes p1 to p9.

15. M1: one agent uses at most 2 of the 4 workers at a time (parallel mode and Assist lanes run invocations of one agent at the same time). The worker read uses poll, not select.

16. After the merge of task-14: the backtest clock reaches the worker; backtest and trial renders pass the agent as owner; regression tests through a backtest and a trial. check_template never runs template code.
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

Security review fixes (task-13 security review, findings H1 to L2). Commits 57bfdb4 and 7428b11.

Structural fix: each rule template renders in a separate worker process (rules/render_pool.py, rules/render_worker.py). A worker is one Python process (python -I -m hagent_app.rules.render_worker).
- The worker starts with an empty environment (TZ only) and the working directory /. Thus it does not get the HA token from the environment.
- It sets RLIMIT_AS (its address space after the imports plus 256 MiB), RLIMIT_CORE 0, and before each render a soft RLIMIT_CPU of the CPU time used plus the time limit plus 2 s.
- The app kills and replaces a worker that does not answer within the time limit plus 0.5 s. A worker that reports a memory error is not used again.
- The app sends a pickle of plain data only (the scoped state snapshot, the time zone key, the variables through plain_data). The worker answers with JSON in length-prefixed frames, so the app never reads a pickle from a worker.
- The pool has up to 4 workers. One agent uses at most 2 at a time. The pool runs its blocking work in its own threads, so it does not depend on one event loop.
- rules/__init__.py now imports lazily (PEP 562). A worker then uses about 25 MB RSS and starts in about 60 ms. With the eager package import, a worker loaded most of the app (about 120 MB, about 0.9 s).

In-process first line, and the regression tests (app/tests/test_rule_render_pool.py unless named):
- H1: _Deadline is a BaseException. A render whose trace function was removed fails. Tests: test_h1_a_caught_deadline_never_yields_a_result, test_h1_the_worker_ends_an_escaped_render.
- H2: deep_size reads Namespace content and dict views; _guard sizes them. Tests: test_h2_the_size_walk_sees_namespaces_and_dict_views (8 cases), test_the_worker_has_a_memory_limit.
- H3: _Sandbox.concat counts the length and stops at MAX_OUTPUT before the join (also for set blocks). Tests: test_h3_the_output_limit_holds_while_jinja_joins, test_h3_many_small_parts_stay_within_the_limit.
- M1: Tests: test_m1_a_stuck_c_call_is_killed_and_the_pool_recovers (5 stuck regex renders, then {{ 1 + 1 }} renders), test_m1_one_agent_cannot_hold_every_worker.
- M2: the format check refuses a * width or precision. Tests: test_m2_a_star_width_fails (5 cases), test_m2_a_normal_format_still_works.
- L1: int * int checks the bit length of the product against MAX_BITS. Test: test_l1_integer_multiplication_has_a_size_limit.
- L2: the result cache key holds ScopedStates.fingerprint() (a digest of the visible entity IDs). Test: test_rules.py::test_a_cached_result_does_not_outlive_a_scope_change (it fails without the fix). check_writes now runs before the cache lookup, as defence in depth.
- Other tests: test_the_worker_does_not_get_the_app_environment, test_a_template_error_keeps_the_worker, test_plain_data_holds_no_objects_with_methods. The old monkeypatch test of the thread pool is removed.

This supersedes the known limit 'one slow C call cannot be stopped; the thread runs on in a pool of 4'. The app now kills such a worker.

Residual risks:
- The worker runs as the app user with no file system or seccomp sandbox. A template that escaped the Jinja sandbox could read the files of the app.
- The lazy rules/__init__.py can conflict with task-14 if task-14 adds exports there.

Validation: mise run check passes (ruff, mypy strict, 1011 app tests, integration tests, frontend checks). In this worktree, integration/.venv-ha2611 had a damaged pytz (no __init__.py). uv sync --reinstall-package pytz fixed it. This was an environment fault, not a code fault.

Merge of impl (task-14, backtests and trials) and follow-up fixes. Commits 64abb41 (merge) and d6384df.

- The merge broke test_backtest.py::test_now_is_the_time_of_the_past_invocation. The worker rebuilt a plain TemplateClock from the time zone, so the fixed time of backtest.evaluate.FixedClock was lost. TemplateClock now has an optional field at (a fixed time). The render pool sends it to the worker. utcnow() follows the clock too. FixedClock becomes the function fixed_clock(tz, at).
- DryRunEvaluator and the trial runner pass owner=agent.id, so the per-agent worker limit covers them. The dry-run path has no result cache. It runs check_writes before the action, as the live stage does.
- Tests: test_backtest.py::test_backtest_renders_have_the_worker_limits (a stuck regex is killed in each case, a namespace bomb fails, a * width in the agreement template fails), test_trial_renders_have_the_worker_limits, test_utcnow_is_the_time_of_the_past_invocation.

New finding (not in the review): Jinja's optimizer, and its constant folding of {{ }} output, ran filters and tests with constant arguments at compile time. check_template runs in the app process at self.create. Thus {{ ('aaa...!') is match('(a+)+$') }} blocked the event loop: 32 s for 30 characters, doubling with each character. Fix: the sandbox sets optimized=False, a code generator folds only literal text and constants, check_template uses a name tracker with no optimizer, and the check refuses {% autoescape %} (Jinja computes its argument at compile time). Tests: test_rule_render_pool.py::test_the_check_never_runs_template_code (3 cases), test_autoescape_is_refused, test_constant_output_still_renders.

Residual: the per-agent cap does not separate backtests from live rules. Backtests of several agents can use all 4 workers for a short time. Each render ends within the time limit plus 0.5 s, so this causes a delay, not a stall.

Validation: mise run check passes (1085 app tests, integration tests, frontend checks).

Lead: an independent review found 3 high, 2 medium and 2 low issues (sandbox resource limits, cache scope). task-13-fix fixed all seven: templates render in a worker process with hard memory, CPU and time limits, and the cache key holds a scope digest. It also found and fixed an event-loop ReDoS through Jinja constant folding at create time. Residual: the worker has no filesystem/seccomp sandbox.
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

Security review fixes (H1 to L2): rule templates now render in worker processes with memory, CPU and wall-clock limits, an empty environment and a JSON reply. The app kills and replaces a stuck worker, and one agent uses at most 2 of the 4 workers. In-process checks now also cover a caught deadline, namespaces and dict views, the joined output, * format widths and int * int. The result cache key holds a digest of the visible entity IDs. Verified with mise run check (1011 app tests), including a regression test for each review probe.

After the merge of task-14, the same worker limits cover backtests and trials (fixed backtest clock sent to the worker, per-agent owner). check_template no longer runs template code through Jinja's constant folding, which had let a constant regex block the event loop at create time. Verified with mise run check (1085 app tests), including regression tests through a backtest and a trial.
<!-- SECTION:FINAL_SUMMARY:END -->
