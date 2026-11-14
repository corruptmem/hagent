---
id: TASK-3
title: 'Spike: sources for model pricing and cost metering'
status: To Do
assignee: []
created_date: '2026-11-14 23:03'
labels:
  - spike
  - providers
dependencies: []
references:
  - docs/design.md
priority: high
type: spike
ordinal: 3000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
hagent records cost per call and enforces budgets. OpenRouter reports cost per call; OpenAI may report tokens only. Find a reliable cost source for chat, decision and embedding calls on both providers, including cached-token pricing.

Design: docs/design.md sections 16 and 17.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 For each provider and call type (chat, decision, embedding), the cost source is identified: returned by the API or computed from a price table
- [ ] #2 Cached-token pricing is covered where the provider offers it
- [ ] #3 A proposal for keeping a price table current is documented
- [ ] #4 Findings are recorded in the task notes
<!-- AC:END -->
