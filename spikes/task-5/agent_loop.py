"""Probe: a minimal Pydantic AI agent with one tool, on each provider.

The agent has long, stable instructions (above the provider cache minimum),
one tool that returns a large result, the ``Caching`` capability, and
compaction from Pydantic AI Harness:

- ``TieredCompaction`` with one ``ClearToolResults`` tier. The target is low,
  so the tier must clear old tool results inside the turn.
- ``WarnOnCacheBusts`` warns when the cached prefix collapses between
  requests.

Each target runs two turns. The second turn continues from the history of the
first, so it can read the prefix that the first turn cached. The script prints
the usage of each model request: input, cache read, cache write and output
tokens. It writes the same table to ``samples/``.

Targets:

- ``openai``: OpenAI Responses API, ``gpt-6-luna``.
- ``openrouter-openai``: OpenRouter chat API, ``openai/gpt-6-luna``
  (implicit caching, no markers).
- ``openrouter-anthropic``: OpenRouter chat API, ``anthropic/claude-haiku-5.5``
  (caching needs markers, which ``Caching`` adds).

Run: ``uv run --group spike python spikes/task-5/agent_loop.py [target ...]``
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import warnings
from pathlib import Path
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.capabilities import Caching
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolReturnPart
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.providers.openrouter import OpenRouterProvider
from pydantic_ai.usage import UsageLimits
from pydantic_ai_harness import ClearToolResults, TieredCompaction
from pydantic_ai_harness.warn_on_cache_busts import WarnOnCacheBusts

SAMPLES = Path(__file__).parent / "samples"
PLACEHOLDER = "[tool result cleared]"

# About 3,000 tokens of stable instructions. Deterministic, so the prefix is
# identical across requests and runs.
RULES = "\n".join(
    f"Rule {i}: when sensor group {i % 7} reports a value outside its band, "
    f"note the room, the value and the band, and do not change any device."
    for i in range(1, 121)
)
INSTRUCTIONS = (
    "You are a test agent for a home automation system. Use the read_sensor "
    "tool to read sensors one at a time. Answer in one short sentence.\n\n" + RULES
)


def read_sensor(name: str) -> str:
    """Read one sensor and return its recent history.

    Args:
        name: The sensor name, for example "hall".
    """
    history = ", ".join(f"t-{i}m: {19.0 + (i % 5) * 0.1:.1f} C" for i in range(60))
    return f"sensor {name}: now 18.4 C. History: {history}"


def build_model(target: str) -> Model:
    if target == "openai":
        return OpenAIResponsesModel(
            "gpt-6-luna",
            provider=OpenAIProvider(api_key=os.environ["OPENAI_API_KEY"]),
        )
    provider = OpenRouterProvider(api_key=os.environ["OPENROUTER_API_KEY"])
    if target == "openrouter-openai":
        return OpenRouterModel("openai/gpt-6-luna", provider=provider)
    if target == "openrouter-anthropic":
        return OpenRouterModel("anthropic/claude-haiku-5.5", provider=provider)
    msg = f"unknown target {target}"
    raise ValueError(msg)


def build_agent(model: Model) -> Agent[None, str]:
    return Agent(
        model,
        instructions=INSTRUCTIONS,
        tools=[read_sensor],
        capabilities=[
            Caching(),
            TieredCompaction(
                tiers=[ClearToolResults(max_tokens=1, keep_pairs=1)],
                target_tokens=3_500,
            ),
            WarnOnCacheBusts(),
        ],
    )


def usage_rows(messages: list[ModelMessage]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, ModelResponse):
            u = message.usage
            rows.append(
                {
                    "model": message.model_name,
                    "input": u.input_tokens,
                    "cache_read": u.cache_read_tokens,
                    "cache_write": u.cache_write_tokens,
                    "output": u.output_tokens,
                    "tool_calls": [
                        p.tool_name
                        for p in message.parts
                        if getattr(p, "part_kind", "") == "tool-call"
                    ],
                }
            )
    return rows


def cleared_count(messages: list[ModelMessage]) -> int:
    return sum(
        1
        for m in messages
        for p in m.parts
        if isinstance(p, ToolReturnPart) and p.content == PLACEHOLDER
    )


async def probe(target: str) -> dict[str, Any]:
    agent = build_agent(build_model(target))
    limits = UsageLimits(request_limit=8, total_tokens_limit=60_000)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        first = await agent.run(
            "Read the sensors hall, kitchen and attic, one call each, in that "
            "order. Then tell me the coldest one.",
            usage_limits=limits,
        )
        second = await agent.run(
            "Now read the sensor garage and say if it is colder than the hall.",
            message_history=first.all_messages(),
            usage_limits=limits,
        )
    result = {
        "target": target,
        "turn1": {
            "output": first.output,
            "requests": usage_rows(first.new_messages()),
            "cleared_tool_results": cleared_count(first.all_messages()),
            "run_usage": {
                "input": first.usage.input_tokens,
                "cache_read": first.usage.cache_read_tokens,
                "cache_write": first.usage.cache_write_tokens,
                "output": first.usage.output_tokens,
                "requests": first.usage.requests,
                "cost_usd": str(first.usage.cost),
            },
        },
        "turn2": {
            "output": second.output,
            "requests": usage_rows(second.new_messages()),
            "cleared_tool_results": cleared_count(second.all_messages()),
            "run_usage": {
                "input": second.usage.input_tokens,
                "cache_read": second.usage.cache_read_tokens,
                "cache_write": second.usage.cache_write_tokens,
                "output": second.usage.output_tokens,
                "requests": second.usage.requests,
                "cost_usd": str(second.usage.cost),
            },
        },
        "warnings": [f"{w.category.__name__}: {w.message}" for w in caught],
    }
    SAMPLES.mkdir(exist_ok=True)
    out = SAMPLES / f"agent-{target}.json"
    out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return result


async def main(targets: list[str]) -> int:
    failed = 0
    for target in targets:
        try:
            await probe(target)
        except Exception as exc:  # report and continue
            failed += 1
            print(f"{target}: FAILED: {type(exc).__name__}: {exc}")
    return 1 if failed else 0


if __name__ == "__main__":
    chosen = sys.argv[1:] or ["openai", "openrouter-openai", "openrouter-anthropic"]
    sys.exit(asyncio.run(main(chosen)))
