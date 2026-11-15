---
id: TASK-14
title: Rule backtesting and side-effect-free trials
status: In Progress
assignee:
  - '@claude'
created_date: '2026-11-14 23:04'
updated_date: '2026-11-15 02:07'
labels:
  - app
  - rules
dependencies:
  - TASK-13
references:
  - docs/design.md
priority: high
type: feature
ordinal: 14000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Let agents test rules before and after activation: backtest a draft rule version against recorded history with its agreement template, and run sampled trials whose agent turns write nothing.

Design: docs/design.md sections 10.1 (Agreement, Trial) and 10.7.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 A backtest replays matching past invocations (last 7 days by default) without writes, and stores the agreement rate and cost
- [x] #2 Decision-rule backtests can be limited to a number of invocations
- [x] #3 Trial agent turns write nothing, count towards the budget and stop when it is spent
- [x] #4 History keeps inputs and context for the retention period so that backtests can run
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Migration 0750_backtest.sql: invocation_records (inputs with full input states, recorded context values, decision answers by request hash; retention purge), rule_backtests and rule_backtest_cases, rule_trials.
2. Package app/src/hagent_app/backtest/: recorder (wraps RulesComponent.try_rules, a recording ContextCollector subclass and a recording decision client; stage.py stays untouched), history context (history item via ha.get_statistics with end = invocation time; invocation_history bounded by created_at; action_response and store_file from the record or from turn_tool_calls of the turn; else the case is skipped), dry-run evaluator (fixed clock at the invocation time, recorded input states, decisions replayed or live within limit and budget and marked, writes checked and captured, never sent to the executor; no RuleStore.record, no live cache).
3. Backtester: past agent_turn ok invocations (7 days default, sources that rules can answer), match with context {}, agreement template (rule_result, agent_result; JSON equality without one), counts, agreement rate, exact cost by run ID in current_invocation, BudgetExceededError stops the run and skips the rest. Fallback to turns.input for history before 0750.
4. Trials: RulesComponent samples answered rules (injectable random, one trial at a time per rule), checks the budget, runs a dry-run agent turn in a tracked task with its own run ID, compares with the agreement template, stores the outcome; stop() cancels trial tasks. default_components sets rules.turns and the meter.
5. Rule statistics: rule_stats output adds backtest and trial figures per version (small edit in rules/store.py).
6. Tool self.backtest_rule (writes=True) through the agent_state chain; CASES entry; FakeAgentState method.
7. Management API (rules list/view/versions/stats/rollback, template check, backtests, trials) in docs/api.md; app/README.md section.
8. Tests: FakeHA command log shows no call_service from backtests with writes and from trial turns; budget stop; replay vs live marking; retention boundary; parity of dry-run evaluator with the live engine.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Implementation notes (task-14).

New package app/src/hagent_app/backtest/ (records, evaluate, runner, trials, store, component, api, config) and migration 0750_backtest.sql (invocation_records, rule_backtests, rule_backtest_cases, rule_trials).

Edits outside backtest/ (small, for the parallel security review of rules/):
- rules/component.py: DecisionSource protocol replaces the ProvidersComponent annotation, so that the component gets the recording decision source.
- rules/store.py: RuleStore.version_extras hook in rule_stats; AgentStateStores.backtests and backtest_rule.
- rules/spec.py: one docstring line.
- tools/stores.py: AgentStateStore.backtest_rule. tools/store_tools.py: self.backtest_rule tool (writes=True). scheduler/component.py: SchedulerStateStore forwards backtest_rule.
- app.py: BacktestComponent between rules and router; it is the router rule stage.
- Tests: helpers/tools.py (FakeAgentState.backtest_rule), test_tool_executor.py (two CASES), test_http_server.py (component order), test_rules_component.py (removed a stale type ignore).
- rules/stage.py is not changed.

Decisions:
- Records are written around RulesComponent.try_rules for every invocation that rules can answer, also with no active rule, so a new draft rule has history. Inputs keep the full input states (state_attr works in a replay). A recording ContextCollector subclass replaces RuleEngine.context; a recording decision client wraps the decision source.
- Candidates: agent_turn ok invocations, or invocations with a finished trial (agent result). Rules do not answer review, Assist and test, so they are not candidates. Newest first. limit counts matched invocations.
- Context in a replay: recorded value first; history via ha.get_statistics with end = invocation time; invocation_history bounded by created_at; action_response only from turn_tool_calls of the turn, else the case is skipped (never called live); store_file from the turn, else the current file.
- Decisions: replay of the same request key (model, values, questions), else live (budget, marked live), or skipped with live=false. BudgetExceededError stops the backtest (status budget).
- Writes: check_writes, then the ha.call_service authorize step and the approval check on the rendered data; recorded in the case, never executed, no audit row. No RuleStore.record, no live cache.
- Agreement: the agreement template (rule_result, agent_result); without it, JSON equality. Skipped, declined and failed cases do not count in the rate.
- Cost: backtests and trials run in their own task with current_invocation set to their ID; cost = sum of provider_calls with that ID.
- Retention: records and backtest cases older than backtest.retention_days (30, never below max_days) are purged at start and every 6 hours. invocations and turns stay.

Known limits:
- DryRunEvaluator mirrors RuleEngine._answer and imports _question and _check_store. A security fix to _answer or to those helpers needs the same change in the evaluator. test_the_dry_run_matches_the_live_rule compares the two.
- Entities that are not inputs show their current state in a replay. utcnow() reads the real time. store_file falls back to the current content.
- invocation_history in a replay can include invocations that were not finished at that time.
- Each case can make one ha.get_statistics call; no cache across cases.

Validation: mise run check passes (ruff, mypy strict, 965 pytest tests, frontend checks).

AC evidence (app/tests/test_backtest.py):
- AC1: test_a_backtest_replays_past_invocations_without_writes (no call_service in the FakeHA command log, no audit row, agreement rate and cost in rule stats, live counters 0), test_the_default_window_is_seven_days, test_a_backtest_tests_a_draft_version, test_history_context_ends_at_the_invocation_time, test_now_is_the_time_of_the_past_invocation, test_a_rendered_write_gets_the_service_call_check.
- AC2: test_decision_backtests_can_be_limited, test_decision_backtests_have_a_default_limit, test_recorded_decisions_are_replayed, test_a_spent_budget_stops_the_backtest.
- AC3: test_a_trial_turn_writes_nothing_and_compares (dry_run turn, no call_service), test_a_trial_counts_towards_the_budget_of_the_agent, test_a_spent_budget_stops_trials, test_a_trial_turn_that_hits_the_budget_records_it, test_trials_follow_the_sample_rate, test_one_trial_at_a_time_for_each_rule, test_stop_cancels_running_trials.
- AC4: test_each_invocation_keeps_its_inputs_and_full_states, test_records_keep_the_context_that_a_rule_collects, test_retention_deletes_only_old_records, test_retention_never_drops_below_the_backtest_window, test_history_from_before_records_uses_the_turn_input.
- Scope: test_an_out_of_scope_history_item_never_reaches_ha; CASES self.backtest_rule not_owner and tool_off; test_the_rule_routes_keep_agents_apart.
<!-- SECTION:NOTES:END -->

## Comments

<!-- COMMENTS:BEGIN -->
author: @claude
created: 2026-11-15 02:07
---
Proposed follow-ups (not created): (1) store_file context at the invocation time, from the knowledge item versions. (2) A statistics cache inside one backtest. (3) UI screens for the rule routes (task-19/task-20). (4) A background mode for long decision backtests, if the synchronous API call gets too slow. (5) Record the state of non-input entities that rule templates read, if replays need them.
---
<!-- COMMENTS:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Built backtests and trials (design 10.1, 10.7) in the new backtest package with migration 0750. The rule stage records the inputs, full input states, rule context and decision answers of each invocation that rules can answer, and the records are purged after the retention period. A backtest replays past agent-turn invocations (7 days by default) against a rule version with a fixed clock, recorded states and context from history. Decisions are replayed or run live within a limit and the budget. Writes are checked and recorded, never sent. The backtest stores the agreement rate and the cost in the rule statistics. Sampled trial turns run as dry runs next to answered rules, count towards the budget and stop when it is spent. Added the self.backtest_rule tool and the rule routes (docs/api.md). Verified with mise run check (965 tests), including FakeHA command-log checks for no service calls from backtests and trial turns, cost attribution, budget stops, limits, replay, retention and scope tests.
<!-- SECTION:FINAL_SUMMARY:END -->
