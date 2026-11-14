---
id: TASK-9
title: 'Provider adapters, cost metering and budgets'
status: In Progress
assignee:
  - '@claude'
created_date: '2026-11-14 23:03'
updated_date: '2026-11-14 23:35'
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

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Package hagent_app/providers/. Config section 'providers' (ProvidersConfig): API keys as masked Secret values, base URLs, extra decision models, price overrides, budgets (agent default USD 0.50/day, per-agent overrides, installation budget), drift threshold, refresh switch. KeySource protocol so task-20 can swap the source.
2. Migration 0300: provider_calls (one row per call: tokens, cache read/write, cost in integer pico-USD, cost source, computed cost, drift, price source/version), budget_reservations, openrouter_prices.
3. PriceTable: override > OpenRouter daily layer > genai-prices (calc_price, tiers kept). Fallback openrouter 'vendor/model' -> genai-prices provider 'vendor'. Unknown or dynamic (-1) price: refuse, or reserve a configured maximum.
4. Budget ledger: reserve in one db.run inside BEGIN IMMEDIATE; check agent and installation totals (spent + reserved); settle to actual cost or release on failure; clear stale reservations at start. Day = app local time, clock seam.
5. Decision adapter: direct HTTP to OpenAI /v1/decisions and OpenRouter /api/alpha/decisions with normalised question and answer types (yes/no, choice, score, per-question refusal). App-owned decision model list, extendable in config.
6. Embedding adapter: OpenAI-compatible /embeddings, batch, dimensions, encoding_format float.
7. Chat: factory for OpenAIResponsesModel / OpenRouterModel plus MeteredModel (Pydantic AI WrapperModel) that reserves before each request and request_stream and records usage after. OpenRouter cost from provider_details['cost'], OpenAI from the price table; record drift.
8. Providers component with a daily OpenRouter price refresh (off in tests). One HTTP stack: httpx2 with MockTransport fixtures in tests.
9. Tests: fixtures from spikes/task-5 samples, budget race with asyncio.gather, keys absent from DEBUG logs, errors and FakeHA traffic. mise run check.
<!-- SECTION:PLAN:END -->
