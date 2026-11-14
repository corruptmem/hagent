---
id: TASK-5
title: 'Spike: verify external API details (decisions, embeddings, Pydantic AI)'
status: In Progress
assignee:
  - '@claude'
created_date: '2026-11-14 23:03'
updated_date: '2026-11-14 23:16'
labels:
  - spike
  - providers
dependencies: []
references:
  - docs/design.md
priority: high
type: spike
ordinal: 5000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
The design relies on details seen only in documentation summaries. Verify them with real calls or source before the adapters are built: OpenAI Decisions API request and response format; OpenRouter decisions endpoint (Jev, alpha); embeddings endpoints on both providers; Pydantic AI compaction, prompt caching (including OpenRouter cache markers) and its OpenAI and OpenRouter model support on Python 3.14.

Design: docs/design.md sections 9.5, 10.3, 13.1, 13.2, 13.3 and 16.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 A real decision request succeeds on OpenAI and on OpenRouter (Jev), with multiple questions, and the response format is documented
- [ ] #2 A real embeddings request succeeds on both providers
- [ ] #3 A minimal Pydantic AI agent runs a tool loop on both providers with compaction and prompt caching, and reports cached tokens
- [ ] #4 Any design assumption that proved wrong is corrected in docs/design.md
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Read the current provider docs: OpenAI Decisions guide and reference, OpenRouter Jev and Decisions API, Pydantic AI caching, compaction, decision-model and OpenRouter pages.
2. Write probe scripts under spikes/task-5/: decisions (OpenAI and OpenRouter, raw HTTP, three question types in one request), embeddings (both providers), Pydantic AI agent (tool loop, caching, compaction, cached-token report) on both providers.
3. Run each probe once with cheap models and tiny prompts. Keep total spend under USD 1.50. Save redacted request and response samples next to the scripts.
4. Record findings in the task notes.
5. Correct wrong assumptions in docs/design.md sections 9.5, 10.3, 13.1, 13.2, 13.3 and 16.
6. Add pydantic-ai to the app package. Put other probe-only packages in a spike dependency group.
7. Run mise run check.
<!-- SECTION:PLAN:END -->
