---
id: TASK-9
title: 'Provider adapters, cost metering and budgets'
status: To Do
assignee: []
created_date: '2026-11-14 23:03'
labels:
  - providers
dependencies:
  - TASK-3
  - TASK-5
  - TASK-6
references:
  - docs/design.md
priority: high
type: feature
ordinal: 9000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Build adapters for chat models (through Pydantic AI), decision models (OpenAI Decisions API and OpenRouter decisions) and embeddings on OpenAI and OpenRouter. Record token use, cached tokens and cost per call, and enforce per-agent and installation budgets with cost reservation.

Design: docs/design.md sections 10.3, 13.3, 16 and 17.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Chat, decision and embedding calls work on both providers behind one interface per call type
- [ ] #2 Each call records tokens, cached tokens and cost
- [ ] #3 Per-agent and installation daily budgets are enforced, with cost reserved before each call so parallel calls cannot overspend
- [ ] #4 API keys are never sent to HA or written to logs (tested)
- [ ] #5 Adapters are tested with recorded fixtures, without live API calls
<!-- AC:END -->
