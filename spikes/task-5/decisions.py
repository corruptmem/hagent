"""Probe: one decision request with three questions on each provider.

The script sends the same evidence and the same three questions (a yes/no
question, a choice question and a score question) to:

- OpenAI: ``POST https://api.openai.com/v1/decisions`` (model ``gpt-6-luna``).
- OpenRouter: ``POST https://openrouter.ai/api/alpha/decisions`` (Jev).

The two APIs use different request shapes, so the script builds each body from
one neutral question list. That mapping is what a hagent adapter must do.

The script writes each request and response to ``samples/``. API keys come
from ``OPENAI_API_KEY`` and ``OPENROUTER_API_KEY`` and never go to a file.

Run: ``uv run python spikes/task-5/decisions.py``
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx2 as httpx

SAMPLES = Path(__file__).parent / "samples"

EVIDENCE: dict[str, Any] = {
    "sensor": "living room temperature",
    "value_c": 17.2,
    "target_c": 21.0,
    "occupied": True,
    "window_open": False,
    "time": "07:10",
}

# A neutral question list. kind is one of "yes_no", "choice", "score".
QUESTIONS: list[dict[str, Any]] = [
    {
        "name": "should_heat",
        "kind": "yes_no",
        "text": "Should the heating turn on now?",
        "yes": "The room is occupied, below target and no window is open.",
        "no": "Heating would waste energy or is not needed.",
    },
    {
        "name": "mode",
        "kind": "choice",
        "text": "Which heating mode fits best?",
        "choices": {
            "comfort": "Occupied room, well below target.",
            "eco": "Small gap to target, or nobody is home.",
            "off": "No heating needed.",
        },
    },
    {
        "name": "urgency",
        "kind": "score",
        "text": "How urgent is heating?",
        "levels": [
            ("low", "At or near target."),
            ("medium", "A few degrees below target."),
            ("high", "Far below target and occupied."),
        ],
    },
]

INSTRUCTION = "Decide about home heating from this sensor reading."


def openai_body() -> dict[str, Any]:
    """Map the neutral questions to the OpenAI Decisions API shape."""
    questions: list[dict[str, Any]] = []
    for q in QUESTIONS:
        if q["kind"] == "yes_no":
            questions.append(
                {
                    "type": "predicate",
                    "name": q["name"],
                    "instructions": f"{q['text']} Yes means: {q['yes']}",
                }
            )
        elif q["kind"] == "choice":
            questions.append(
                {
                    "type": "choice",
                    "name": q["name"],
                    "instructions": q["text"],
                    "choices": [
                        {"value": v, "description": d} for v, d in q["choices"].items()
                    ],
                }
            )
        else:
            questions.append(
                {
                    "type": "score",
                    "name": q["name"],
                    "instructions": q["text"],
                    "levels": [
                        {"label": label, "description": d} for label, d in q["levels"]
                    ],
                }
            )
    return {
        "model": "gpt-6-luna",
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": INSTRUCTION},
                    {"type": "input_json", "data": EVIDENCE},
                ],
            }
        ],
        "questions": questions,
    }


def openrouter_body() -> dict[str, Any]:
    """Map the neutral questions to the OpenRouter (Jev) shape."""
    questions: dict[str, Any] = {}
    for q in QUESTIONS:
        if q["kind"] == "yes_no":
            questions[q["name"]] = {
                "type": "noul",
                "instructions": q["text"],
                "criteria": {"true": q["yes"], "false": q["no"]},
            }
        elif q["kind"] == "choice":
            questions[q["name"]] = {
                "type": "choice",
                "instructions": q["text"],
                "criteria": q["choices"],
            }
        else:
            questions[q["name"]] = {
                "type": "score",
                "instructions": q["text"],
                "criteria": [f"{label}: {d}" for label, d in q["levels"]],
            }
    return {
        "model": os.environ.get("JEV_MODEL", "typesafe/jev-1.14"),
        "state": {"instruction": INSTRUCTION, **EVIDENCE},
        "questions": questions,
    }


def call(name: str, url: str, key_env: str, body: dict[str, Any]) -> bool:
    key = os.environ.get(key_env)
    if not key:
        print(f"{name}: {key_env} is not set, skipped")
        return False
    response = httpx.post(
        url,
        json=body,
        headers={"Authorization": f"Bearer {key}"},
        timeout=60,
    )
    SAMPLES.mkdir(exist_ok=True)
    record = {
        "url": url,
        "status": response.status_code,
        "request": body,
        "response": response.json(),
    }
    out = SAMPLES / f"decision-{name}.json"
    out.write_text(json.dumps(record, indent=2) + "\n")
    print(f"{name}: HTTP {response.status_code}, written to {out.name}")
    print(json.dumps(record["response"], indent=2))
    return response.is_success


def main() -> int:
    ok_openai = call(
        "openai",
        "https://api.openai.com/v1/decisions",
        "OPENAI_API_KEY",
        openai_body(),
    )
    ok_openrouter = call(
        "openrouter",
        "https://openrouter.ai/api/alpha/decisions",
        "OPENROUTER_API_KEY",
        openrouter_body(),
    )
    return 0 if ok_openai and ok_openrouter else 1


if __name__ == "__main__":
    sys.exit(main())
