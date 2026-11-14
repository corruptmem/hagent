---
id: TASK-9
title: 'Provider adapters, cost metering and budgets'
status: Done
assignee:
  - '@claude'
created_date: '2026-11-14 23:03'
updated_date: '2026-11-14 23:52'
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
- [x] #1 Chat, decision and embedding calls work on both providers behind one interface per call type
- [x] #2 Each call records tokens, cached tokens and cost
- [x] #3 Per-agent and installation daily budgets are enforced, with cost reserved before each call so parallel calls cannot overspend
- [x] #4 API keys are never sent to HA or written to logs (tested)
- [x] #5 Adapters are tested with recorded fixtures, without live API calls
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Package hagent_app/providers/. Config section 'providers' (ProvidersConfig): API keys, base URLs, extra decision models, price overrides, budgets (agent default USD 0.50/day, per-agent overrides, installation budget), drift threshold, refresh switch. KeySource protocol so that task-20 can change the source. Secret hides the value.
2. Migration 0300: provider_calls (one row per call: tokens, cache read/write, cost in integer pico-USD, cost source, computed cost, drift, price source/version), budget_reservations, openrouter_prices.
3. PriceTable: override > OpenRouter daily layer > genai-prices (calc_price, tiers kept). Fallback openrouter 'vendor/model' -> genai-prices provider 'vendor'. Unknown or dynamic (-1) price: refuse, or reserve a configured amount.
4. Meter: reserve in one db.run inside BEGIN IMMEDIATE; check agent and installation totals (spent + reserved); record the call and delete the reservation in one transaction; clear stale reservations at start. Budget day = app local time, clock seam.
5. Decision adapter: direct HTTP to OpenAI /v1/decisions and OpenRouter /api/alpha/decisions with normalised question and answer types (yes/no, choice, score, per-question refusal). App-owned decision model list, extendable in config.
6. Embedding adapter: OpenAI-compatible /embeddings, batch, dimensions, encoding_format float.
7. Chat: factory for OpenAIResponsesModel / OpenRouterModel plus MeteredModel (Pydantic AI WrapperModel). It reserves before each request and request_stream, and records usage after. It refuses batch and provider-side compaction (not metered). OpenRouter cost from provider_details['cost'], OpenAI from the price table; drift recorded.
8. ProvidersComponent with a daily OpenRouter price refresh (first check 60 s after start; off in tests). One HTTP stack: httpx2, MockTransport in tests.
9. Fixtures: one approved recording run (spikes/task-9/record_fixtures.py) for chat and embeddings; decision fixtures from spikes/task-5/samples.
10. Tests: per call type and provider, budget race with parallel calls, keys absent from DEBUG logs, errors, records and FakeHA traffic. mise run check.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
## Implementation (2026-11-14)

Code: app/src/hagent_app/providers/ (config, prices, meter, http, decisions, embeddings, chat, openrouter_prices, service). Migration: 0300_providers.sql. Docs: app/README.md section 'Providers and cost'.

Terms:
- **Meter**: the class that reserves cost, records each call in provider_calls and deletes the reservation.
- **Reservation**: the expected cost of a call, held against the budgets before the call starts.
- **pico-USD**: 1e-12 USD. Money columns hold integers in this unit. REAL loses the 4.2e-8 USD per-token rates in sums.

Interfaces for task-10 and others:
- ProvidersComponent.chat_model(provider, model, agent_id=...) returns a MeteredModel. Give it to the Pydantic AI Agent. The meter works per model request, not per run.
- ProvidersComponent.decisions.decide(DecisionRequest, agent_id=...).
- ProvidersComponent.embeddings.embed(provider, model, texts, agent_id=..., dimensions=...).
- ProvidersComponent.meter.budget_state(agent_id or None) for the status and cost sensors.
- agent_id=None charges the installation only.
- MeteredModel refuses batch_create and compact_messages: both are paid calls outside the meter. Task-10 must use local compaction strategies (pydantic-ai-harness ClearToolResults and similar), or extend the meter first.
- The adapters do not retry. ProviderHTTPError has a retryable flag.

Behaviour:
- Budget day = local date of the app. The Supervisor sets the app time zone to the HA time zone (confirmed by t3). Meter takes a clock argument.
- The reservation check and insert run in one BEGIN IMMEDIATE transaction on the single database thread.
- A call can cost more than its reservation. The meter records the real cost.
- A failed call records status 'error'. If the provider reported usage, the row keeps the billed cost.
- A stream that stops before its usage event counts the reservation as cost (cost_source 'reserved').
- Unknown or dynamic price: PriceUnknownError before any HTTP request, unless unknown_price_reserve_usd is set.
- Drift: (reported - computed) / computed is stored for each OpenRouter call. A warning is logged above drift_warn_ratio (default 5%). The fixtures show -1% (OpenRouter bills 0.99 x the list price).

Fixtures (AC5):
- t3 approved one recording run under USD 0.01. spikes/task-9/record_fixtures.py recorded raw, redacted bodies for chat (OpenAI Responses and OpenRouter, plain and streamed) and embeddings (both providers) on gpt-4o-mini and text-embedding-3-small. IDs, billing and user fields are removed.
- The first attempt failed in my recording transport after the provider answered. The OpenAI client retried, so a few extra tiny calls were billed. Total spend stays far below USD 0.01 (each call cost about USD 0.000004).
- Decision fixtures are the raw task-5 samples with the response ID redacted.

Edits outside the providers area (t3 must reconcile at merge):
- app/src/hagent_app/app.py: default_components() returns [ProvidersComponent()] (local import because of an import cycle).
- app/tests/test_migrations.py: three tests assumed that only migration 0001 exists. They now compare with the discovered package migrations and allow versions below 1000.
- app/tests/conftest.py: new providers_env fixture.
- app/pyproject.toml: explicit genai-prices>=0.2.3 and httpx2>=2.16 (both were transitive through pydantic-ai).

Decisions for the lead:
- installation_daily_budget_usd defaults to USD 5.00. The brief gave no default.
- genai-prices UpdatePrices is not started. The genai-prices price version is therefore always 'snapshot'. The OpenRouter layer and overrides cover the gap.
- A bad price_overrides or decision_models entry raises ValueError at component start, so the app does not start. ConfigError would be more uniform.
- OpenAI decision cache tokens are priced at the full input rate (task-3 open item). A price override fixes this when the rate is verified.
- Proposal sent to t3: Config.options has the default dataclass repr, so repr(ctx.config) shows the keys. One-line fix in the core area: repr=False on Config.options.

Validation: mise run check passed (ruff, mypy strict, pytest: 100 passed).

Core fix (approved by t3): app/src/hagent_app/config.py now sets repr=False on Config.ha_token and Config.options. The other Config fields (ha_url, data_dir, port, log_level) hold no secrets. Tests: test_config.py::test_repr_hides_token_and_options and test_app.py::test_app_context_repr_hides_secrets. This is a fifth edit outside the providers area.

AC evidence:
- AC1: test_providers_chat.py::test_chat_on_both_providers (4 cases: OpenAI and OpenRouter, plain and streamed), test_providers_decisions.py::test_openai_decision_answers_and_cost and ::test_openrouter_decision_answers_cost_and_drift, test_providers_embeddings.py::test_embeddings_on_both_providers (2 cases).
- AC2: the same tests assert tokens and cost on each provider_calls row. Cache tokens: test_openai_cache_tokens_reach_the_record (cost equals the task-3 observed USD 0.00020385), test_openrouter_cache_tokens_reach_the_record (cache read and cache write), test_openai_decision_cache_tokens_reach_the_record.
- AC3: test_providers_budget.py::test_parallel_calls_cannot_overspend_agent_budget (10 parallel calls, 3 fit, 3 reservations held at the same time), ::test_installation_budget_covers_all_agents_and_none, ::test_budget_resets_on_the_next_local_day, ::test_failed_call_releases_its_reservation, ::test_cancelled_call_releases_its_reservation; test_providers_chat.py::test_reservation_uses_max_tokens (refused before any HTTP request).
- AC4: test_providers_keys.py (DEBUG logs incl. openai/httpx2 loggers, error texts, call records, request bodies, FakeHA commands), plus the Config repr tests.
- AC5: every provider test uses FakeProviders (httpx2 MockTransport) with recorded fixtures. No test makes a network call.

Lead: merged with task-7 (component order: providers, agents, http). Accepted defaults: installation budget USD 5.00/day; OpenAI decision cache tokens at full rate until verified; invalid price_overrides fail at start. genai-prices UpdatePrices is not started; task-20 settings can enable it.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Built the providers package. Chat calls use Pydantic AI models (OpenAIResponsesModel, OpenRouterModel) in a MeteredModel wrapper. Decision calls go directly over HTTP to the OpenAI Decisions API and the OpenRouter decisions endpoint, with one request and answer type for both. Embedding calls use the OpenAI-compatible endpoint on both providers. Each call writes one provider_calls row with input, cache-read, cache-write and output tokens and the cost. The cost is OpenRouter usage.cost, or the price table (overrides, daily OpenRouter lists, genai-prices); OpenRouter drift is stored. The meter reserves the expected cost in one transaction before each call and checks the per-agent (default USD 0.50) and installation daily budgets; the budget day is local time. Keys come from a KeySource, are hidden in repr (also Config) and are redacted from errors. Verified with 102 passing tests (mise run check) that replay recorded, redacted provider responses; no test makes a network call. Edits outside the area: app.py, config.py, conftest.py, test_migrations.py, app/pyproject.toml.
<!-- SECTION:FINAL_SUMMARY:END -->
