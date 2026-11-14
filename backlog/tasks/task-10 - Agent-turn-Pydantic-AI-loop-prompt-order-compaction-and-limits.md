---
id: TASK-10
title: 'Agent turn: Pydantic AI loop, prompt order, compaction and limits'
status: To Do
assignee: []
created_date: '2026-11-14 23:03'
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
- [ ] #1 The prompt follows the order in section 9.5, and a test shows the stable prefix is identical across invocations
- [ ] #2 Large tool results are cut to a size limit and kept in full in history
- [ ] #3 Running summaries per source and per Assist conversation are written by the summary model
- [ ] #4 Main files near their size limit trigger a request to the agent to shorten them
- [ ] #5 Structured results are validated against the schema, with one correction attempt
- [ ] #6 A turn stops at its tool-call or token limit and reports this to the router
<!-- AC:END -->
