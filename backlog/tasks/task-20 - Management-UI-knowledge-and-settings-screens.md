---
id: TASK-20
title: 'Management UI: knowledge and settings screens'
status: In Progress
assignee:
  - '@claude'
created_date: '2026-11-14 23:04'
updated_date: '2026-11-15 00:54'
labels:
  - ui
dependencies:
  - TASK-18
  - TASK-11
references:
  - docs/design.md
priority: medium
type: feature
ordinal: 20000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Build the knowledge area (stores, documents, files, attachments, change history) and the settings area (providers, API keys, embedding model, installation budget, approvers).

Design: docs/design.md section 7.2 (Knowledge, Settings) and 13.4.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Stores can be created and edited, documents uploaded, replaced and deleted, and files browsed, edited and restored
- [ ] #2 The UI shows which agents attach each store
- [ ] #3 API keys are shown only masked
- [ ] #4 Selecting an embedding model states that text is sent to the provider, and a change starts a re-index
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Back end: new settings package (migration range 1000-1099, table app_settings). SettingsComponent starts before providers. Routes GET/PATCH api/settings, PUT/DELETE api/settings/keys/{provider}. Keys return only a masked hint and their source.
2. Providers read keys through a live settings KeySource (settings, then env, then options). Meter budgets, price overrides, decision models and the genai-prices auto-update (default off) follow settings changes without restart. ProvidersConfig keeps its fields as fallback.
3. Approvals seam: settings hold approvers, notify-service overrides and timeout with the task-16 checks; a reader returns that shape. task-16 is not in this base; the switch happens at merge (asked t3).
4. Tests: masking (GET, errors, events, logs), live key use, budgets, overrides, approvals checks. Docs: docs/api.md, app/README.md.
5. Front end: Knowledge area (store list with attached agents, create/edit/delete store, items, upload/replace/delete documents, file edit with version conflict handling, history and restore, read-only for large or document items). Settings area (keys masked, embedding model with provider notice and re-index status, budgets, approvers, price overrides, auto-update toggle, decision models). Agent editor store picker from the stores list. Events invalidate knowledge and settings queries.
6. vitest tests for each AC; mise run check.
<!-- SECTION:PLAN:END -->
