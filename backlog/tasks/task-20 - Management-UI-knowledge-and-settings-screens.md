---
id: TASK-20
title: 'Management UI: knowledge and settings screens'
status: To Do
assignee: []
created_date: '2026-11-14 23:04'
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
