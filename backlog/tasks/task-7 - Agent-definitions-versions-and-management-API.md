---
id: TASK-7
title: 'Agent definitions, versions and management API'
status: In Progress
assignee:
  - '@claude'
created_date: '2026-11-14 23:03'
updated_date: '2026-11-14 23:35'
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
- [ ] #1 Create, edit, copy, turn off and delete agents through the API, with validation of every definition field in section 7.1
- [ ] #2 Each change creates a new definition version; earlier versions can be listed, compared and restored
- [ ] #3 Deleting an agent asks whether to keep or delete its private store
- [ ] #4 No agent tool can write a definition (tested)
- [ ] #5 The API contract is documented
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
