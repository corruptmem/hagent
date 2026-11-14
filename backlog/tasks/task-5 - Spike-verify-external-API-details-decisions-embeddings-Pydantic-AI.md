---
id: TASK-5
title: 'Spike: verify external API details (decisions, embeddings, Pydantic AI)'
status: In Progress
assignee:
  - '@claude'
created_date: '2026-11-14 23:03'
updated_date: '2026-11-14 23:24'
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
- [x] #1 A real decision request succeeds on OpenAI and on OpenRouter (Jev), with multiple questions, and the response format is documented
- [x] #2 A real embeddings request succeeds on both providers
- [x] #3 A minimal Pydantic AI agent runs a tool loop on both providers with compaction and prompt caching, and reports cached tokens
- [x] #4 Any design assumption that proved wrong is corrected in docs/design.md
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

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Probes: spikes/task-5/ (decisions.py, embeddings.py, agent_loop.py, decisions_pydantic_ai.py). Recorded requests and responses: spikes/task-5/samples/. Run date 2026-11-14. Python 3.14.3, pydantic-ai 2.68.0, pydantic-ai-harness 0.68.0, openai 3.51.0.

Decisions, OpenAI (AC1):
- POST https://api.openai.com/v1/decisions, model gpt-6-luna (the only model). HTTP 200.
- Request: model, input (text, or messages with input_text, input_json and input_image parts), questions (array, 1 to 400).
- Question types: predicate {name, instructions}; choice {name, instructions, choices: [{value, description}], 2 to 255}; score {name, instructions, levels: [{label, description}], 2 to 10}.
- Response: {model, answers, usage}. answers is an array in question order. predicate -> {type, name, probability}. choice -> {type, name, choice, probabilities: [{value, probability}], confidence}. score -> {type, name, score, probabilities: [{value, label, probability}], confidence}. Any question can return {type: refusal, name}.
- usage: input_tokens, input_tokens_details {cached_tokens, cache_write_tokens}, output_tokens (0), total_tokens. No cost field. Price USD 0.10 per 1M input tokens. Output is free.
- Prompt caching: implicit breakpoint at the end of input, minimum 1,024 tokens. Questions are outside the cached prefix.

Decisions, OpenRouter Jev (AC1):
- POST https://openrouter.ai/api/alpha/decisions, model typesafe/jev-1.14 (resolved typesafe/jev-1.14-20261014). HTTP 200. typesafe/jev-1.13 and ~typesafe/jev-latest also exist.
- Request: model, state (text, JSON object or array), questions (object keyed by name).
- Question types: noul {instructions, criteria: {true, false}} (optional criteria); choice {instructions, criteria: {option: description}}; score {instructions, criteria: [level descriptions]}. 1.14 also has box (image location).
- Response: {id, model, provider, answers, usage}. answers is an object keyed by name. noul -> {type, noul} (probability of yes, no confidence). choice -> {type, choice, probabilities: {option: p}, confidence}. score -> {type, score, legend: {"0": text}, probabilities: {"0": p}, confidence}.
- usage: input_tokens, output_tokens, cost (USD), cost_details.
- A second route, POST /api/v1/systemone, takes the TypeSafe SDK shape.
- Jev decision models are not in GET /api/v1/models (only typesafe/jev-router).
- Same input, urgency score: OpenAI 1.09 (confidence 0.84), Jev 1.51 (confidence 0.26). Thresholds do not carry over between models.

Embeddings (AC2):
- OpenAI text-embedding-3-small and OpenRouter openai/text-embedding-3-small, batch of 2, dimensions=256. Both HTTP 200, 2 vectors of 256, norm 1.0, near-identical values.
- The OpenAI Python client asks for base64 by default. Pass encoding_format=float or decode.
- OpenRouter usage has cost. OpenAI usage has only prompt_tokens and total_tokens.

Pydantic AI agent (AC3):
- Agent with long instructions (about 4,000 tokens), one tool, Caching(), TieredCompaction([ClearToolResults]) and WarnOnCacheBusts. Two turns: the second continues the first turn's history.
- Targets: OpenAIResponsesModel gpt-6-luna; OpenRouterModel openai/gpt-6-luna; OpenRouterModel anthropic/claude-haiku-5.5 (needs cache markers).
- All three ran the tool loop (4 to 5 requests in turn 1). Each reported cache_read_tokens and cache_write_tokens for each request. Example, OpenAI turn 1: request 1 wrote 4,071 tokens, requests 2 to 4 read 4,071 each. Turn 2 request 1 read 4,841. Anthropic through OpenRouter read 5,395 after the first request, so the Caching markers work.
- RunUsage.input_tokens includes cache-read and cache-write tokens.
- RunUsage.cost was set on all targets, also on OpenAI where the response has no cost.
- Compaction: ClearToolResults cleared 2 to 5 tool results per run.
- Caveat: target_tokens=3500 is below the instructions size, so compaction ran on every request. The model then said earlier readings were not available. This proves compaction runs, and it shows the cache cost: after each clear, cache reads fell back to the instructions prefix. A real configuration needs a target above that floor and min_clear_tokens.
- WarnOnCacheBusts stayed silent. The largest miss was 770 tokens, below its default 2,000-token threshold.

Pydantic AI decision models (extra):
- OpenAIDecisionsModel (gpt-6-luna) and SystemOneModel with SystemOneProvider(base_url=https://openrouter.ai/api/v1) for Jev both ran. Output came from a Pydantic model. provider_details has confidence (per field), probabilities (choice fields) and scores.
- A bool field gives a confidence margin, not the probability of yes (OpenAI raw API 1.0, Pydantic AI confidence 0.94 on a similar prompt).

Design corrections (AC4), docs/design.md:
- 16: Pydantic AI does support decision endpoints. Listed the trade-offs and left the adapter choice open. Named the chat model classes. Jev is not in the model list.
- 10.3: three question types with limits, answer fields, per-question refusal, response shapes of both APIs, thresholds per model.
- 13.1: compaction strategies come from pydantic-ai-harness (0.x, pin it), not core Pydantic AI.
- 13.2: Caching capability adds the markers. WarnOnCacheBusts is in the harness, with its thresholds. Cache writes cost 1.25x. Record cache-read and cache-write counts. Probe evidence for the compaction and cache trade-off.
- 13.3: embeddings request details, base64 default, cost only from OpenRouter.
- 9.5: tool definitions come before instructions, and providers fix that order.

Dependencies: pydantic-ai-slim[openai,openrouter]>=2.68 added to hagent-app. pydantic-ai-harness>=0.68 added to the root spike dependency group only.

Spend: about USD 0.02 in total. OpenRouter daily usage was USD 0.0087 after the first runs, and the reruns added about USD 0.002. OpenAI usage was about 60k input tokens on gpt-6-luna and 15 embedding tokens.

Checks: mise run check passed (ruff, mypy, pytest).
<!-- SECTION:NOTES:END -->

## Comments

<!-- COMMENTS:BEGIN -->
author: @claude
created: 2026-11-14 23:24
---
Proposals for the lead (not created as tasks):
1. Decide the decision adapter: Pydantic AI decision classes (OpenAIDecisionsModel, SystemOneModel) or direct HTTP to both endpoints. The spike leans to direct HTTP for section 10.3: agents write questions at run time, the result template needs raw probabilities, and refusals are per question. See design section 16.
2. Move pydantic-ai-harness from the spike group to the app when the agent runtime task starts. It is 0.x, so pin it.
3. Decide how the management UI offers decision models, because the OpenRouter model list does not include Jev.
---
<!-- COMMENTS:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Verified the external APIs with real calls on 2026-11-14 for about USD 0.02. The OpenAI Decisions API and the OpenRouter Jev decisions endpoint both answered a three-question request (yes/no, choice, score). Their request and response shapes are in the task notes and in spikes/task-5/samples/. Embeddings worked on both providers with the same request. A Pydantic AI 2.68 agent on Python 3.14 ran a tool loop on OpenAI, OpenRouter-OpenAI and OpenRouter-Anthropic. It used Caching and harness compaction, and reported cache-read and cache-write tokens and cost. Corrected docs/design.md sections 9.5, 10.3, 13.1, 13.2, 13.3 and 16. The main correction: Pydantic AI now has decision model classes. The adapter choice stays open for the lead. mise run check passes.
<!-- SECTION:FINAL_SUMMARY:END -->
