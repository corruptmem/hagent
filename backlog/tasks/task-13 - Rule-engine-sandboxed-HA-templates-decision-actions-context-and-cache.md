---
id: TASK-13
title: 'Rule engine: sandboxed HA templates, decision actions, context and cache'
status: In Progress
assignee:
  - '@claude'
created_date: '2026-11-14 23:04'
updated_date: '2026-11-15 01:04'
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
- [ ] #1 Templates cannot read entities outside the agent's scope; states() returns unknown for them (tested)
- [ ] #2 The supported HA template subset is documented and tested against HA's own output for common templates
- [ ] #3 Decision actions send one request with one or more questions, using inputs, caller data and context
- [ ] #4 Context items (history, action_response, store_file, invocation_history) respect scope and cache times
- [ ] #5 Rules cannot write outputs that need approval
- [ ] #6 Every rule change creates a new version with its own statistics; rollback works
- [ ] #7 Template rules work while HA Core restarts and while the internet is down
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Add jinja2 to app deps. New package app/src/hagent_app/rules/.
2. rules/spec.py: strict pydantic RuleSpec (extra=forbid): name, match, context items (history | action_response | store_file | invocation_history, each with cache_seconds), action (template | decision with questions and an include selector for inputs/data/context), result template (may return 'decline'), writes (entity_id, service, data templates), cache (seconds, keys), agreement, trial (stored for task-14).
3. rules/templates.py: ImmutableSandboxedEnvironment with a documented HA subset (states, is_state, state_attr, is_state_attr, has_value, now, utcnow, as_timestamp, as_datetime, float/int/bool filters, iif, min/max/average, round, regex_*, to_json/from_json, timedelta ...). States view filtered to the agent read scope (ha: none -> inputs only); out-of-scope states() returns 'unknown'. Static check of names/filters/tests at create time (unknown function -> invalid_spec). HA-style result parsing (literal_eval).
4. Migration 0700_rules.sql: rules (owner, position, state draft|active|off, current_version, deleted_at), rule_versions (immutable spec JSON), rule_stats per (rule, version).
5. rules/store.py: RuleStore implements AgentStateStore for kind 'rule' (other kinds -> unavailable), versions, reorder, rollback, stats; also rule_owner/rule_stats for TurnHistory (replace stubs).
6. SPEC_CHECKS['rule']: validate spec; check context reads with check_read, action_response and writes with check_service_call (through CallService.authorize) and needs_approval -> deny writes that need approval; template static check; decision needs models.decision.
7. rules/stage.py RuleStage: read active rules once; per rule: match -> cache -> context (through ToolExecutor without approval gate: ha.get_statistics, ha.call_service return_response with a response-only service, knowledge.read; invocation_history from invocations of the same agent and source) -> action (template, or one decision request via providers.decisions with retry; failure -> decline) -> result -> writes through ToolExecutor (scope + audit, approval -> denied) -> stats (matched, answered, declined, cache_hits, errors, cost).
8. New self tools: self.reorder_rules, self.rollback_rule (owner checks, CASES entries). Management API: GET rules of an agent, POST rollback; docs/api.md.
9. RulesComponent in default_components; router.rules and tools.agent_state wiring; update test_http_server list.
10. HA-output check: record HA render_template outputs for synthetic templates (no real entity) into fixtures with a one-off read-only script; tests compare sandbox output to the fixtures.
11. Tests: scope (out-of-scope states unknown, iteration filtered), approval writes denied at create and fire, versions/rollback/stats, context cache + scope, decision request shape (FakeProviders), template rules with stale HA cache and no provider. Docs: docs/rule-templates.md, app/README section. mise run check.
<!-- SECTION:PLAN:END -->
