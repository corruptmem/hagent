"""Probe: Pydantic AI decision models on both providers.

Design section 16 assumed that Pydantic AI does not support the decision
endpoints. Pydantic AI 2.68 has decision model classes. This script checks two
of them with one request each:

- ``OpenAIDecisionsModel``: OpenAI ``POST /v1/decisions``.
- ``SystemOneModel`` with ``SystemOneProvider`` pointed at OpenRouter: the
  OpenRouter ``POST /api/v1/systemone`` route, which serves Jev.

The output type has a yes/no field and a choice field. The script prints the
output and ``provider_details``, which holds the probabilities.

Run: ``uv run python spikes/task-5/decisions_pydantic_ai.py``
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.models.openai_decisions import OpenAIDecisionsModel
from pydantic_ai.models.system_one import SystemOneModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.providers.system_one import SystemOneProvider

SAMPLES = Path(__file__).parent / "samples"


class Heating(BaseModel):
    should_heat: bool = Field(description="The heating should turn on now.")
    mode: Literal["comfort", "eco", "off"] = Field(
        description="The heating mode that fits best."
    )


PROMPT = json.dumps(
    {"room": "living room", "value_c": 17.2, "target_c": 21.0, "occupied": True}
)


def models() -> list[tuple[str, Model]]:
    return [
        (
            "openai",
            OpenAIDecisionsModel(
                "gpt-6-luna",
                provider=OpenAIProvider(api_key=os.environ["OPENAI_API_KEY"]),
            ),
        ),
        (
            "openrouter",
            SystemOneModel(
                os.environ.get("JEV_MODEL", "typesafe/jev-1.14"),
                provider=SystemOneProvider(
                    base_url="https://openrouter.ai/api/v1",
                    api_key=os.environ["OPENROUTER_API_KEY"],
                ),
            ),
        ),
    ]


def main() -> int:
    failed = 0
    results: dict[str, Any] = {}
    for name, model in models():
        agent = Agent(model, output_type=Heating)
        try:
            run = agent.run_sync(PROMPT)
        except Exception as exc:  # report and continue
            failed += 1
            results[name] = {"error": f"{type(exc).__name__}: {exc}"}
            continue
        results[name] = {
            "output": run.output.model_dump(),
            "model_name": run.response.model_name,
            "provider_details": run.response.provider_details,
            "usage": {
                "input": run.usage.input_tokens,
                "cache_read": run.usage.cache_read_tokens,
                "output": run.usage.output_tokens,
                "cost_usd": str(run.usage.cost),
            },
        }
    SAMPLES.mkdir(exist_ok=True)
    out = SAMPLES / "decision-pydantic-ai.json"
    out.write_text(json.dumps(results, indent=2, default=str) + "\n")
    print(json.dumps(results, indent=2, default=str))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
