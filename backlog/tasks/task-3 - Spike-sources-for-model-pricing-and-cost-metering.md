---
id: TASK-3
title: 'Spike: sources for model pricing and cost metering'
status: In Progress
assignee:
  - '@claude'
created_date: '2026-11-14 23:03'
updated_date: '2026-11-14 23:23'
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
- [x] #1 For each provider and call type (chat, decision, embedding), the cost source is identified: returned by the API or computed from a price table
- [ ] #2 Cached-token pricing is covered where the provider offers it
- [x] #3 A proposal for keeping a price table current is documented
- [x] #4 Findings are recorded in the task notes
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Write stdlib-only probe scripts under spikes/task-3/ (ruff-clean, keys from environment only).
2. Probe OpenRouter: models endpoint pricing fields; chat call with usage accounting (cost, cached tokens); repeated long prompt to see cache hits; embeddings call usage and cost; generation stats endpoint.
3. Probe OpenAI: models endpoint (check for prices); Responses API usage fields (cached_tokens) with a repeated long prompt; embeddings usage; check for any cost field.
4. Check decision-call cost sources from docs and, if cheap, a minimal call; read task-5 notes, do not wait on it.
5. Keep total spend under USD 0.50 with tiny prompts and cheap models.
6. Record findings and a price-table refresh proposal in the task notes.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
## Findings (2026-11-14)

Probe scripts: `spikes/task-3/`. All calls used small prompts. Total spend was under USD 0.02.

Terms:
- **Price table**: a local table of USD prices per token for each provider and model.
- **Reservation**: the cost estimate that the runtime holds against the budget before a call (design section 17).
- **Cache read**: input tokens that the provider served from its prompt cache. **Cache write**: input tokens that the provider stored in its prompt cache.
- **genai-prices**: the price library that Pydantic AI uses to fill `RequestUsage.cost`.

### Cost source for each provider and call type

| Provider | Call type | Cost in response? | Recorded-cost source | Reservation source |
|---|---|---|---|---|
| OpenRouter | chat | yes: `usage.cost` | `usage.cost` | price table |
| OpenRouter | decision (Jev) | yes: `usage.cost` | `usage.cost` | price table |
| OpenRouter | embedding | yes: `usage.cost` | `usage.cost` | price table |
| OpenAI | chat (Responses API) | no, tokens only | price table | price table |
| OpenAI | decision (Decisions API) | no, tokens only | price table | price table |
| OpenAI | embedding | no, tokens only | price table | price table |

The response arrives after the call, so a reservation always needs a price table. This applies to both providers.

OpenRouter:
- Chat sends `usage.cost`, `usage.cost_details.upstream_inference_{prompt,completions}_cost`, `prompt_tokens_details.cached_tokens` and `prompt_tokens_details.cache_write_tokens`.
- `usage.cost` was 0.99 × `upstream_inference_cost` on every chat call. The cause is not known. hagent must store `usage.cost`, because that is the amount billed to the key.
- Embeddings send `usage.cost` and `prompt_tokens`. The response has no cached-token field.
- Decisions (`POST /api/alpha/decisions`, model `~typesafe/jev-latest`) send `usage.cost`, `input_tokens` and `output_tokens`. The field names differ from chat (`prompt_tokens`). Output tokens are free.
- `GET /api/v1/generation?id=` gives `total_cost`, `cache_discount` and native token counts for chat. The record appears some seconds after the call. For embedding and decision IDs, the endpoint returned no record.

OpenAI:
- No call type returns a cost. The only related field is `billing.payer`. The response headers have no cost field.
- `GET /v1/models/{id}` has no price fields.

### Price-table formula check

- Cost = tokens × listed price, with separate rates for uncached input, cache read, cache write and output.
- This formula with OpenRouter's `/models` prices gave the exact `upstream_inference_*_cost` for gpt-4o-mini (uncached and cached) and for gpt-6-luna (cache write and cache read).
- Jev cost was exactly 4.2e-8 USD per input token on two calls (311 and 3,601 tokens). This is USD 0.042 per million, observed.
- genai-prices 0.2.3 computed the same value as every observed upstream cost: chat, cache read, cache write, Jev and embedding (`spikes/task-3/probe_genai_prices.py`).

### Cached-token pricing (AC #2)

- OpenRouter `/models` has these price fields: `prompt`, `completion`, `input_cache_read`, `input_cache_write`, `input_cache_write_1h`, `internal_reasoning`, `web_search` and others.
- 75 models have `overrides`: a tier list with `min_prompt_tokens` (for example 272,000) and higher rates above the threshold.
- `typesafe/jev-router` lists `prompt` and `completion` as `-1`, which means dynamic price. A price table must refuse or cap these.
- The `:batch` variants have their own prices.
- OpenRouter chat, gpt-4o-mini: the second call read 2,432 of 2,571 tokens from the cache and cost 52% of the first call.
- OpenRouter chat, gpt-6-luna: the first call wrote 2,567 tokens to the cache at 1.25 × the prompt rate. Later calls read them at 0.1 × the prompt rate.
- OpenAI Responses API reports `input_tokens_details.cached_tokens`, `cache_write_tokens` and `cache_write_tokens_details.{ttl_30m_tokens, ttl_12h_tokens}`. gpt-4o-mini reported no cache writes. gpt-6-luna on the Decisions API reported 2,563 cache-write tokens (30 m TTL), then cache reads.
- OpenAI caching is best effort. Without `prompt_cache_key` and with 2 s between calls, the second gpt-4o-mini call missed the cache. gpt-5-nano hit only on the third call. Reservations must use the uncached rate.
- Jev on OpenRouter showed no caching. Three identical 3,601-token calls cost the same.

Open question: the OpenAI Decisions API cache rates.
- The base input price is USD 0.10 per million (genai-prices, secondary reports). This is the same as gpt-6-luna chat input.
- Secondary reports disagree on cache rates. One says there is no cache discount. A later report says USD 0.01 per million for cache reads and that cache writes are charged.
- genai-prices lists only the input price for `openai-decisions`. It charges cached tokens at the full rate.
- OpenAI's pricing pages returned HTTP 403 to a script.
- The rate stays unverified. The OpenAI usage dashboard or the admin Costs API can settle it.

### Pydantic AI

- Pydantic AI 2.68.0 (checked in a throwaway environment) copies OpenRouter `usage.cost` and `cost_details` into `ModelResponse.provider_details`.
- `RequestUsage.cost` comes from genai-prices, not from the provider.
- The chat adapter can read the OpenRouter cost from `provider_details["cost"]`. For OpenAI it can use `RequestUsage.cost`.

### Proposal: keep the price table current (AC #3; not built)

1. Use genai-prices as the base price table.
   - It already ships with Pydantic AI.
   - It covers OpenAI chat and embeddings, `openai-decisions`, `typesafe` (Jev), OpenRouter, tiers and cache read/write.
   - Its `UpdatePrices` class fetches a newer JSON snapshot from the genai-prices GitHub repository in the background.
2. Add an OpenRouter layer.
   - Fetch `GET /api/v1/models` and `GET /api/v1/embeddings/models` once a day.
   - Store each row with the fetch date. OpenRouter rows override genai-prices for `openrouter` calls.
   - Model the fields `prompt`, `completion`, `input_cache_read`, `input_cache_write`, `input_cache_write_1h` and `overrides`. Treat `-1` as dynamic.
3. Add a user override in the management UI. It covers rates that no source lists or that a source lists wrong, for example the OpenAI decision cache rates.
4. Order of precedence: user override, then OpenRouter daily fetch, then genai-prices update, then the bundled genai-prices snapshot.
5. Unknown or dynamic price: refuse the call, or reserve at a stated maximum cost per call. Show the reason in the status sensor.
6. Detect drift.
   - For each OpenRouter call, compare the computed cost with `usage.cost`. Log a warning above a set difference, for example 5% (the observed 1% gap must not warn).
   - For OpenAI, an optional daily check against the admin Costs API is possible. This is not tested. It needs an admin key.
7. Record the price-table version or fetch date with each cost record, so that old costs stay explainable.

### Notes for other tasks

- Task-5: the request shapes used here came from error messages of the live endpoints. OpenAI `/v1/decisions` needs `input` and `questions[].{type, instructions, choices[].value}`. OpenRouter `/api/alpha/decisions` needs `state` and `questions` as a record with `{type, question, criteria}`. Task-5 owns the full format.
- Design section 16 says that each adapter reports cost. For OpenAI this cost is computed, not reported. Section 17 "Decision actions bill input tokens only" is true for Jev. For OpenAI decisions, cache-write billing is not verified. This task does not change docs/design.md.
<!-- SECTION:NOTES:END -->

## Comments

<!-- COMMENTS:BEGIN -->
author: @claude
created: 2026-11-14 23:23
---
Review: AC #2 stays unchecked. Cache read and cache write rates are verified for OpenRouter (chat, Jev) and for OpenAI chat and embeddings (the computed cost matches the OpenRouter upstream cost exactly). The OpenAI Decisions API cache rates are not verified: secondary sources disagree, genai-prices lists only the input rate, and OpenAI's pricing pages block scripts. To close it, compare a few cached decision calls with the OpenAI usage dashboard or the admin Costs API. Possible design follow-ups (not done here): section 16 says each adapter reports cost, but for OpenAI the adapter computes it; section 17 'decision actions bill input tokens only' needs a note on OpenAI decision cache writes.
---
<!-- COMMENTS:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Probed real usage and cost fields on OpenRouter and OpenAI for chat, decision and embedding calls (spikes/task-3/, spend under USD 0.02). OpenRouter returns usage.cost for all three call types. OpenAI returns tokens only, including cached_tokens and cache_write_tokens with TTL buckets. tokens × listed price reproduced every observed upstream cost exactly, and so did genai-prices (Pydantic AI's price library). Both providers need a price table for budget reservation. Proposed a refresh scheme: genai-prices with its updater as the base, a daily OpenRouter /models layer, a UI override, and drift checks against usage.cost. Open: the OpenAI Decisions API cache rates are unverified, so AC #2 stays unchecked.
<!-- SECTION:FINAL_SUMMARY:END -->
