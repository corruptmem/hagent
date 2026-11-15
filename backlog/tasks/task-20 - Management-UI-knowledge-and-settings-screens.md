---
id: TASK-20
title: 'Management UI: knowledge and settings screens'
status: In Progress
assignee:
  - '@claude'
created_date: '2026-11-14 23:04'
updated_date: '2026-11-15 01:14'
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
- [x] #1 Stores can be created and edited, documents uploaded, replaced and deleted, and files browsed, edited and restored
- [x] #2 The UI shows which agents attach each store
- [x] #3 API keys are shown only masked
- [x] #4 Selecting an embedding model states that text is sent to the provider, and a change starts a re-index
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

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Implementation (commits b772ce4, dfafcc0, ffee354, after merge of impl with task-16):
- Back end: new settings package (app/src/hagent_app/settings/), migration 1000_settings.sql (new range 1000-1099, README range table and migration test updated). Routes GET/PATCH api/settings, PUT/DELETE api/settings/keys/{provider}, documented in docs/api.md (Settings) with the settings change-event topic. SettingsComponent starts first in default_components(); test_http_server.py expected list updated.
- Keys: SettingsKeySource reads on each call. Order: settings, then environment, then options. The API returns only {set, hint, source}; the hint is the last 4 characters of a key with 20 or more characters. Keys are stored as plain text in the app database (encryption at rest was not required).
- Providers: budgets (installation and default agent), price overrides, decision models (built-in plus extras) and the genai-prices auto-update (default off, UpdatePrices injectable for tests) follow settings changes without a restart. ProvidersConfig keeps every field as the fallback, plus genai_prices_auto_update.
- Approvals (task-16): ApprovalsComponent takes settings. Stored approval settings replace approvers, notify_services and timeout_seconds; with none stored, the approvals option section applies. dev_trust_user_header stays an option. Changes apply to the next request (ApprovalService.set_config, MobileAppNotifier.set_overrides).
- UI: Knowledge list (attached agents per store), store view (edit, delete with store_in_use text, items, upload, replace and delete documents, new file, file editor with base_version and conflict reload, history with view and restore, open a path to restore a deleted file), read-only view for items that arrive in parts. Settings screen (masked keys, embedding model with provider notice and re-index text, budgets, approvers with notify overrides, price overrides, auto-update toggle, decision models). Agent editor store picker from api/knowledge/stores, with the store ID field as fallback. Events invalidate knowledge and settings queries.
Validation: mise run check passed (ruff, mypy, 606 pytest, eslint, prettier, tsc, 40 vitest). Live smoke test: the app started locally and served GET/PATCH api/settings, PUT api/settings/keys/openai (response held only the hint) and POST api/knowledge/stores; the app log held no key. Browser check not done: the Chrome extension was not connected.

AC evidence:
- AC1: frontend/src/screens/knowledge/Knowledge.test.tsx: creates a store and opens it; edits the name, description and size limit; uploads, replaces and deletes a document; edits a file with its version and handles a conflict; creates a file; shows an earlier version and restores it; restores a deleted file opened by its path; encodes each part of a path; does not edit a file that arrives in parts. The back-end steps of the deleted-file restore (history after delete, restore without base_version) are covered by test_write_checks_versions_and_records_authors in app/tests/test_knowledge_stores.py; a read of a missing item returns 404 not_found (knowledge/service.py _read).
- AC2: Knowledge.test.tsx: shows each store with the agents that attach it; shows the agents that attach the store; loads the stores again after a knowledge or agents event.
- AC3: app/tests/test_settings.py: test_get_shows_keys_only_masked, test_short_key_has_no_hint, test_invalid_key_errors_hold_no_key, test_providers_use_the_settings_key_at_once (errors and logs hold no key). Settings.test.tsx: shows API keys only masked and stores a new key. Live smoke test: the PUT response held only the hint.
- AC4: Settings.test.tsx: states that text goes to the provider before it changes the embedding model (notice, re-index text, PUT api/knowledge/settings). Re-index on a model change: test_embeddings_are_queued_deduplicated_and_charged in app/tests/test_knowledge_search.py (line 279 onward asserts vectors only of the new model).
- Approvals switch: test_settings_approvers_replace_the_option_section, test_settings_timeout_and_override_apply in app/tests/test_approvals.py.
<!-- SECTION:NOTES:END -->

## Comments

<!-- COMMENTS:BEGIN -->
author: @claude
created: 2026-11-15 01:10
---
Proposed follow-ups (not created): 1) a route that lists deleted paths of a store, so the UI can offer restore without typing the path; 2) an HA users list in the catalogue for an approver picker (the UI now takes user IDs as text); 3) the meter uses the default agent budget from settings and agent_budgets_usd, not budget.daily_usd of each definition (set_agent_budgets has no caller); 4) the definition default budget (0.50) does not follow the settings default agent budget.
---
<!-- COMMENTS:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Added the knowledge and settings areas of the management UI, and an app-managed settings store. The settings package stores API keys, budgets, price overrides, the genai-prices auto-update, decision models and approvers in the app database. The API shows keys only as a hint. Providers read keys through a live key source (settings, then environment, then options), and budgets, prices and models follow settings changes without a restart. Approvals read approvers, notify overrides and the time limit from settings, with the option section as fallback. The UI has a store list with attached agents, a store view (edit, delete, upload, replace, file edit with conflict handling, history and restore), a settings screen with the embedding-model notice, and a store picker in the agent editor. Verified with mise run check (606 pytest, 40 vitest, ruff, mypy, eslint, prettier, tsc) and a local smoke test of the new routes.
<!-- SECTION:FINAL_SUMMARY:END -->
