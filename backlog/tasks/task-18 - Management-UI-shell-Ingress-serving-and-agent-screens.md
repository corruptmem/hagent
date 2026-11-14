---
id: TASK-18
title: 'Management UI: shell, Ingress serving and agent screens'
status: In Progress
assignee:
  - '@claude'
created_date: '2026-11-14 23:04'
updated_date: '2026-11-14 23:51'
labels:
  - ui
dependencies:
  - TASK-7
references:
  - docs/design.md
priority: medium
type: feature
ordinal: 18000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Create the React single-page application (TanStack Router, Query and Table, built with Vite), served by the app through Ingress, with the agent definition screens. Set up front-end linting, formatting and type checking in pre-commit and mise.

Design: docs/design.md sections 6.1, 7.1, 7.2 (Agents) and 22 (Management UI stack).
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 The UI loads through Ingress with relative URLs and the X-Ingress-Path base path
- [ ] #2 The app accepts UI connections only from 172.30.32.2
- [ ] #3 All definition fields in section 7.1 can be edited, with entity pickers from live HA data
- [ ] #4 Definition versions can be compared and restored
- [ ] #5 A test invocation runs with sample data without changes to HA
- [ ] #6 Front-end lint, format and type checks run in pre-commit and mise run check
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Back end: IngressGuard component (new package hagent_app/ingress): middleware checks request.remote against config section 'ingress.allowed_addresses' (default 172.30.32.2, CIDR allowed), fails closed before start, exempt-prefix hook for the later integration link.
2. Back end: UI static serving in the same package: catch-all route that excludes api/, serves files from 'ingress.ui_dir', SPA fallback to index.html with an escaped <base href> from X-Ingress-Path, traversal guard, clear page when the build is missing.
3. Back end: read-only HA catalog endpoint GET api/ha/catalog (package hagent_app/ha_catalog): entities from the state cache plus device/area/label/floor registries and services; short TTL cache; stale flag when HA is offline.
4. Python tests for guard, static serving, catalog (read-only proof via FakeHA). Fix existing tests that use default_components with a config override.
5. Front end: frontend/ with Vite, React, TS strict, TanStack Router/Query/Table; agents list, editor for every 7.1 field with pickers, versions list/diff/restore, test-invocation panel against a documented planned endpoint; change-event WebSocket.
6. Tooling: pin npm in mise, frontend check task in mise run check, setup runs npm ci, local pre-commit hooks for eslint/prettier/tsc; vitest tests incl. a test that every defaults leaf path has a form control.
7. Docs: docs/api.md (catalog, UI paths, planned test-invocation contract), app/README.md, AGENTS.md, task-21 build notes.
<!-- SECTION:PLAN:END -->
