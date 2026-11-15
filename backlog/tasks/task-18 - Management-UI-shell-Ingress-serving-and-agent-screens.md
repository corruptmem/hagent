---
id: TASK-18
title: 'Management UI: shell, Ingress serving and agent screens'
status: Done
assignee:
  - '@claude'
created_date: '2026-11-14 23:04'
updated_date: '2026-11-15 01:00'
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
- [x] #1 The UI loads through Ingress with relative URLs and the X-Ingress-Path base path
- [x] #2 The app accepts UI connections only from 172.30.32.2
- [x] #3 All definition fields in section 7.1 can be edited, with entity pickers from live HA data
- [x] #4 Definition versions can be compared and restored
- [x] #5 A test invocation runs with sample data without changes to HA
- [x] #6 Front-end lint, format and type checks run in pre-commit and mise run check
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

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Decisions:
- IngressGuard is a component in hagent_app/ingress. Its middleware refuses all requests with 503 until start reads the ingress options. It checks the TCP peer address (request.remote), never a header. Option ingress.allowed_addresses accepts addresses and CIDR networks. The default is 172.30.32.2 only. exempt_prefixes (empty) is the hook for the integration link; it matches whole path segments.
- UiComponent serves the UI on the route /{tail:(?!api(?:/|$)).*}. The pattern excludes api/, so it never hides a later API route, and an unknown api/ path still returns JSON 404. The app adds <base href=X-Ingress-Path/> to index.html. It accepts only a plain path of safe characters in the header and uses / for any other value. Vite builds with base ./. Option ingress.ui_dir selects the build; empty means frontend/dist in the checkout.
- GET api/ha/catalog (hagent_app/ha_catalog) joins the state cache with six fixed read commands: config/{entity,device,area,floor,label}_registry/list and get_services. Results stay for 30 s. When HA is offline or a command fails, the endpoint returns the last data with stale=true.
- Knowledge attachments: edit by store ID only (option A, approved by t3). KnowledgeEditor takes an optional stores list for task-11/task-20.
- Test invocation: the runtime does not exist. docs/api.md holds the planned contract for POST api/agents/{id}/test. The UI calls it and reports that the runtime is not available on 404/503/501.
- Tooling: npm 11.17.0 pinned in mise (listed before node, so that it takes precedence) and in package.json packageManager. mise run check depends on a frontend task (eslint, prettier, tsc, vitest). mise run setup runs npm ci. Local pre-commit hooks run eslint, prettier and tsc for frontend/ changes. frontend/dist is in .gitignore; frontend/README.md has the build steps for task-21.
- TanStack Table v9 is the current release. Its FlexRender remounts cells when the column definitions change, so the screens memoise their columns. A vitest case guards this.

Evidence:
- pytest: 174 passed (test_ingress.py, test_ha_catalog.py, test_frontend_fixture.py, updated test_http_server.py).
- vitest: 20 passed (base path, ingress basepath and change events, agents list, editor, versions, test panel).
- pre-commit run --all-files: all hooks passed. mise run check: passed.
- Live run of the app with the real UI build against a live HA instance (scratch data directory, no HA changes): all six registry commands returned non-empty data and stale was false; index.html had the base element for a test X-Ingress-Path; the hashed asset returned 200; api/missing returned JSON 404; an unsafe header gave base /; a LAN source got 403 forbidden; loopback (allowed by the override) got 200.
- The browser pass did not run, because the Chrome extension was not connected. vitest drives the UI flows in jsdom.

Lead: merged into impl with providers component first. Task stays In Progress: AC #5 (test invocation) waits for the agent runtime (task-12); the lead will finish it after task-12 merges.

Lead: AC #5 is done by task-12 (POST api/agents/{id}/test, dry run, sample states, no HA changes; tests in task-12). The UI contract did not change.
<!-- SECTION:NOTES:END -->

## Comments

<!-- COMMENTS:BEGIN -->
author: @claude
created: 2026-11-15 00:12
---
Proposed follow-ups (not created): (1) task-10/12: implement POST api/agents/{id}/test as docs/api.md describes; then check AC #5 of task-18. (2) task-11/task-20: pass the store list to KnowledgeEditor (stores prop in Editor, src/screens/AgentEditor.tsx). (3) Integration link task: use its own port, or add its path to IngressGuard(exempt_prefixes) and authenticate each connection. (4) task-21: build the UI in the image (npm ci && npm run build in frontend/), copy dist/ and set ingress.ui_dir; see frontend/README.md.
---
<!-- COMMENTS:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Added the management UI and its Ingress serving. Back end: IngressGuard (only 172.30.32.2 by default, overridable for development, fails closed), UiComponent (UI files under the X-Ingress-Path base, SPA fallback, api/ excluded) and the read-only GET api/ha/catalog for the entity pickers. Front end: frontend/ with React, TypeScript strict, TanStack Router/Query/Table and Vite; agents list (create, copy, turn on/off, delete with private-store choice), an editor with a control for every definition field and pickers from HA data, versions with view, compare and restore, a test-invocation panel and live change events. eslint, prettier and tsc run in pre-commit; mise run check runs them and vitest. Verified with pytest (174), vitest (20), pre-commit on all files and a live run against HA. AC #5 stays open: the agent runtime (task-10/12) does not exist; docs/api.md holds the planned POST api/agents/{id}/test contract and the UI reports that the runtime is not available.
<!-- SECTION:FINAL_SUMMARY:END -->
