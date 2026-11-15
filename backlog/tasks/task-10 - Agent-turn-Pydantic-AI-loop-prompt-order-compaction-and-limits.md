---
id: TASK-10
title: 'Agent turn: Pydantic AI loop, prompt order, compaction and limits'
status: Done
assignee:
  - '@claude'
created_date: '2026-11-14 23:03'
updated_date: '2026-11-15 00:37'
labels:
  - app
dependencies:
  - TASK-7
  - TASK-8
  - TASK-9
references:
  - docs/design.md
priority: high
type: feature
ordinal: 10000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Run agent turns with Pydantic AI: build the prompt in the cache-friendly order, run the tool loop through the tool executor, compact context, keep running summaries per source, and stop at the per-turn tool-call and token limits.

Design: docs/design.md sections 9.5, 11, 13.1 and 13.2.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 The prompt follows the order in section 9.5, and a test shows the stable prefix is identical across invocations
- [x] #2 Large tool results are cut to a size limit and kept in full in history
- [x] #3 Running summaries per source and per Assist conversation are written by the summary model
- [x] #4 Main files near their size limit trigger a request to the agent to shorten them
- [x] #5 Structured results are validated against the schema, with one correction attempt
- [x] #6 A turn stops at its tool-call or token limit and reports this to the router
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Move pydantic-ai-harness into hagent-app, pinned ==0.68.0. Add jsonschema (pinned) for structured result validation.
2. New package app/src/hagent_app/turns/ with migration 0400 (STRICT tables): turns, turn_tool_calls (full results), turn_compactions, source_summaries.
3. TurnHistory: read interface for task-19 (list, get with tool calls and compactions, recent per source, summary) and the HistoryStore protocol for memory.search, filtered by agent_id in SQL. Plug it into ToolsComponent.history.
4. Prompt builder (design 9.5): parts 2-4 form one deterministic instructions string (instructions, stores with sorted index capped at 50, main files, rules/scopes summary). Parts 5-7 go into the user prompt (summary + latest records, recall placeholder, invocation). Tool definitions come only from ToolExecutor.tools_for; dotted names map to model-safe names.
5. TurnRunner.run_turn(agent snapshot, TurnInput) -> TurnResult: one entry point for task-12. Pydantic AI Agent with Caching, WarnOnCacheBusts and TieredCompaction(ClearToolResults) with min_clear_tokens. UsageLimits from budget.max_tool_calls and max_tokens; stop reasons tool_call_limit, token_limit, budget, provider_error, invalid_result, cancelled.
6. Tool wrapper: each call goes to ToolExecutor.execute, the full result goes to history, the model gets a result cut to a size limit with a marker.
7. Structured results: StructuredDict plus a jsonschema output validator with one correction attempt.
8. Running summaries per source and per Assist conversation: after a turn, the summary model folds older records into the summary; the prompt keeps the latest N records.
9. Main files at 80% of their size limit: the invocation part asks the agent to shorten them; record a compaction event.
10. Register TurnsComponent in default_components, update test_http_server, README section. Tests with FunctionModel/TestModel and fakes only.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Package: app/src/hagent_app/turns/ (config, models, history, prompt, runner, summaries, component). Migration 0400_turns.sql: turns, turn_tool_calls, turn_compactions, source_summaries (STRICT).
Entry point for task-12: TurnsComponent.run_turn(agent snapshot, TurnInput) -> TurnResult (status ok/stopped/failed/cancelled, stop_reason tool_call_limit/token_limit/request_limit/budget/provider_error/invalid_result/error). It does not raise for model, provider or limit failures. A cancelled turn records cancelled and raises CancelledError again.
Prompt (design 9.5): tools from ToolExecutor.tools_for, with provider-safe names (ha.get_state -> ha__get_state). Parts 2-4 form the Pydantic AI instructions: definition instructions, stores (description, access, sorted index capped at index_max_entries, MAIN.md), rules/schedules/scopes. Only id, name, title, description and enabled of each rule, schedule or subscription go in, so volatile fields do not break the cache. Parts 5-7 form the user prompt: running summary plus latest records, recall (only if retrieval is on, through knowledge.search), invocation. The builder reads stores and agent state only through the executor.
Compaction: tool results over tool_result_max_chars are cut for the model and kept in full in turn_tool_calls. TieredCompaction(ClearToolResults(min_clear_tokens, keep_pairs)) runs inside the turn. Caching and WarnOnCacheBusts are on. Only local strategies, because MeteredModel refuses provider-side compaction.
Running summaries: after each non-dry-run turn, SummaryWriter (background task) folds records older than recent_records into source_summaries with the summary model of the definition (default: chat model). Source key: source, or assist:<conversation ID>. The fold stops before the earliest running turn of the key (parallel mode).
Structured results: StructuredDict does not enforce the schema (probe), so a jsonschema Draft 2020-12 output validator raises ModelRetry. retries output=1 gives one correction attempt.
Limits: UsageLimits(tool_calls_limit=budget.max_tool_calls, total_tokens_limit=budget.max_tokens, request_limit=None). A probe showed that Pydantic AI never runs more tool calls than the limit.
History: TurnHistory has the read interface for task-19 (list_turns, get, summary, recent) and implements HistoryStore; TurnsComponent sets ToolsComponent.history. HistoryIndex protocol for task-11: index_history(agent_id, record), index_conversation(agent_id, conversation_id, record), search_history(agent_id, query, filters, limit). Set turns.history.index. Without it, search uses a LIKE match filtered by agent_id. rule_owner returns None and rule_stats returns {} until task-13. On start, TurnsComponent marks turns left running as failed (design 9.6).
Dependencies: pydantic-ai-harness==0.68.0 and jsonschema==4.26.0 moved or added to hagent-app; the spike group is removed; types-jsonschema is in the dev group.
Open points: (1) cost: provider_calls has no invocation or turn link, so TurnResult has no cost_usd; proposal: add an invocation_id column to provider_calls and pass it to MeteredModel. (2) No retry with backoff on chat failures (design 18). (3) The main-file size limit comes from turns.main_file_max_chars until task-11 supplies one per store. (4) Approval gate (task-16) blocks a tool call for as long as an approver takes; the turn has no own timeout.
Validation: mise run check passed (ruff, mypy strict, 404 pytest tests, frontend checks). app/tests/test_turns.py has 25 tests with FunctionModel and the tool harness; no provider or HA calls.

AC evidence (app/tests/test_turns.py, all pass): AC1 test_prompt_follows_section_9_5_and_prefix_is_stable, test_recall_goes_after_history_and_before_the_invocation, test_rule_state_changes_do_not_break_the_stable_prefix. AC2 test_large_tool_result_is_cut_and_kept_in_full, test_in_turn_compaction_clears_old_tool_results. AC3 test_summary_model_writes_running_summary_per_source, test_each_assist_conversation_has_its_own_summary. AC4 test_main_file_near_limit_asks_the_agent_to_shorten_it (85 and 79 of 100 chars). AC5 test_structured_result_gets_one_correction, test_structured_result_fails_after_one_correction. AC6 test_turn_stops_at_its_tool_call_limit, test_turn_stops_at_its_token_limit (TurnResult.status=stopped with stop_reason is the report to the router).

Lead: merged. Open items (1) invocation link for per-turn cost and (2) chat retry with backoff (design 18) are passed to task-12. (3) per-store main-file limit goes to task-11/20. (4) rule stubs go to task-13.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Added the turns package. TurnsComponent.run_turn(agent, TurnInput) is the entry point for the router. It builds the prompt in the order of design section 9.5 with a stable prefix, runs the Pydantic AI tool loop through ToolExecutor, cuts large tool results for the model and keeps them in full, compacts inside the turn with pydantic-ai-harness, validates structured results with one correction, and stops at the per-turn tool-call and token limits with a stop reason. The summary model writes running summaries per source and per Assist conversation. Migration 0400 adds the history tables. TurnHistory gives task-19 a read interface and serves memory.search. Verified with 25 tests in app/tests/test_turns.py (FunctionModel, fake stores, FakeHA) and mise run check. Open: per-turn cost link, chat retries, per-store main-file limit.
<!-- SECTION:FINAL_SUMMARY:END -->
