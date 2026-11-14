---
id: TASK-7
title: 'Agent definitions, versions and management API'
status: In Progress
assignee:
  - '@claude'
created_date: '2026-11-14 23:03'
updated_date: '2026-11-14 23:47'
labels:
  - app
dependencies:
  - TASK-6
references:
  - docs/design.md
priority: high
type: feature
ordinal: 7000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Store agent definitions with immutable versions, create the private knowledge store with each agent, and expose a JSON management API (plus change-event WebSocket) for the UI. Define the management API contract as part of this task.

Design: docs/design.md sections 7, 7.1, 10.6 and 22 (Management UI stack).
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Create, edit, copy, turn off and delete agents through the API, with validation of every definition field in section 7.1
- [x] #2 Each change creates a new definition version; earlier versions can be listed, compared and restored
- [x] #3 Deleting an agent asks whether to keep or delete its private store
- [x] #4 No agent tool can write a definition (tested)
- [x] #5 The API contract is documented
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Add pydantic as a direct app dependency for definition validation.
2. http/ package: HttpServer component (aiohttp). Routes register before start via add_routes(); outer middleware slot via add_middleware() for the task-18 Ingress source-IP check; JSON error middleware; ChangeEvents pub/sub with a /api/events WebSocket and in-process listeners. HttpServer goes last in default_components so it starts last and stops first.
3. agents/ package: AgentDefinition pydantic models (frozen, extra=forbid) for every section 7.1 field with defaults and cross-field rules; migrations 0100 (agents, agent_versions) and 0101 (minimal knowledge_stores record for task-11); AgentService (create, edit with base_version check, copy, enable/disable, delete with required keep/delete choice for the private store, list/get versions, diff, restore); AgentReader read-only interface for task-8.
4. Management API routes under /api/agents, plus /api/agents/schema and /api/agents/defaults.
5. Tests: definition validation, service, API via aiohttp test client, events WebSocket, server lifecycle, reader has no write path and returns frozen definitions.
6. Docs: docs/api.md (STE), app/README.md updates (HTTP server, route hook, knowledge-store hand-off, read interface).
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Implementation (commits 0af3b68, 3d822bf, 0abb1e7):
- http/ package: HttpServer component (aiohttp). Other areas add routes with add_routes() and middleware with add_middleware() in default_components(), before start. HttpServer stays last in the component list. The JSON error middleware runs first, so an ApiError from a later middleware becomes JSON. Task-18 adds the Ingress source-address check with add_middleware(). The integration link connects from HA Core, so that check must exempt the link path, or the link must use its own port.
- ChangeEvents: in-process publish and subscribe, sent to the UI on the /api/events WebSocket. A slow client loses its connection. Call publish on the event-loop thread only.
- agents/ package: AgentDefinition (pydantic, frozen, strict, extra fields rejected, lists as tuples) for every field of section 7.1. Migration 0100 creates agents and agent_versions. Migration 0101 creates the minimal knowledge_stores record for task-11 (hand-off in app/README.md).
- AgentService writes (management API only). AgentReader reads (list_agents, get, get_version) and is the interface for task-8 and the router.
Decisions:
- Delete is a soft delete. The agent row and its versions stay for history. A partial unique index frees the name.
- enabled is outside the versioned definition. A change to it adds no version. A definition equal to the current one adds no version.
- Delete needs private_store=keep|delete. Without it, the API returns 400 private_store_choice_required. keep makes the store a shared store (private=0, no owner). delete returns 409 if other live agents attach the store.
- Copy gets a new private store. Its private attachment points to the new store. Shared attachments stay.
- Restore adds a new version and validates the old definition again. Edit and restore take an optional base_version and return 409 on a mismatch.
- Validation checks formats only. It does not check that HA entities exist. Store IDs are checked against the table.
- Defaults that the design does not give: budget.max_tokens 200000, scheduling.debounce_seconds 5, review cron 0 4 * * *. A structured result needs a schema with type object.
- Added pydantic as a direct dependency and the pydantic.mypy plugin.
Edits outside the task-7 packages (possible merge conflicts): app/tests/test_migrations.py (no hard-coded [1]), app/tests/test_main.py (HAGENT_PORT=0), root pyproject.toml (mypy plugin), AGENTS.md (docs/api.md line), app.py default_components.
AC4 limit: the tests prove at interface level that AgentReader has no write method and returns frozen results. Python can still reach the private _db attribute. result.schema and fallback.value are plain dicts: a caller can change its own copy, but storage stays unchanged (tested). The full guarantee needs task-8 to give tools only AgentReader.
Validation: mise run check passes (ruff, mypy strict, 150 tests).

AC evidence:
- AC1: test_agent_definition.py (56 invalid cases, at least one for each row of section 7.1 and each rule between fields; 15 valid cases) and test_agents_api.py (create, edit, copy, turn off, delete).
- AC2: test_edit_creates_versions, test_unchanged_edit_adds_no_version, test_edit_checks_base_version, test_diff_and_restore, test_restore_revalidates_stores.
- AC3: test_delete_asks_about_private_store, test_delete_and_keep_store, test_delete_and_delete_store, test_delete_store_that_others_attach.
- AC4: test_agent_reader.py (see the AC4 limit above).
- AC5: docs/api.md; test_api_doc.py checks that the document names every served route.

Correction: test_agent_definition.py has 54 invalid cases, not 56.
<!-- SECTION:NOTES:END -->

## Comments

<!-- COMMENTS:BEGIN -->
author: @claude
created: 2026-11-14 23:47
---
Proposed follow-up work (not created): (1) a management API endpoint that lists live HA entities, devices, areas and labels for the UI pickers (section 7.2); (2) a test invocation endpoint (section 7.2), after the router and agent turn tasks. Both are outside the task-7 acceptance criteria.
---
<!-- COMMENTS:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Added agent definitions with immutable versions, a private knowledge store record for each agent, the HTTP server component with a route and middleware hook, the agents management API and the /api/events change-event WebSocket. docs/api.md holds the API contract. app/README.md describes the route hook, the AgentReader interface for task-8 and the knowledge-store hand-off to task-11. Verified with mise run check: ruff, strict mypy and 150 tests pass. The tests cover definition validation, the API end to end through a real server with FakeHA, the reader immutability and the route documentation.
<!-- SECTION:FINAL_SUMMARY:END -->
