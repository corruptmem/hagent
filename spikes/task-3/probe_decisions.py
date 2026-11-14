"""Probe usage and cost fields of the decision endpoints on both providers.

Request shapes come from error messages of the live endpoints on 2026-11-14.
Task-5 owns the full request and response format. This probe only checks
usage, cost and cached tokens. It repeats a long input three times to test
prompt caching. Expected spend per run: under USD 0.002.
"""

import time

from _http import call, dump
from probe_chat import PREFIX

OPENROUTER_DECISIONS = "https://openrouter.ai/api/alpha/decisions"
STATE = PREFIX + "\nThe sky is clear at noon."


def openai_decision() -> None:
    body = {
        "model": "gpt-6-luna",
        "input": STATE,
        "questions": [
            {
                "type": "choice",
                "instructions": "Is the sky blue?",
                "choices": [{"value": "yes"}, {"value": "no"}],
            }
        ],
        "prompt_cache_key": "hagent-task3-decision",
    }
    for attempt in (1, 2, 3):
        resp, _ = call("openai", "/decisions", body)
        dump(f"openai decision attempt {attempt}", resp.get("usage", resp))
        time.sleep(6)


def openrouter_decision() -> None:
    body = {
        "model": "~typesafe/jev-latest",
        "state": STATE,
        "questions": {
            "sky_blue": {
                "type": "choice",
                "question": "Is the sky blue?",
                "criteria": {"yes": "The sky is blue.", "no": "The sky is not blue."},
            }
        },
    }
    gen_id = ""
    for attempt in (1, 2, 3):
        resp, _ = call("openrouter", OPENROUTER_DECISIONS, body)
        dump(f"openrouter decision attempt {attempt}", resp.get("usage", resp))
        gen_id = resp.get("id", "")
        time.sleep(6)
    gen, _ = call("openrouter", f"/generation?id={gen_id}")
    data = gen.get("data", gen)
    keys = ("total_cost", "usage", "native_tokens_prompt", "native_tokens_cached")
    dump("openrouter /generation for decision", {k: data.get(k) for k in keys})


def main() -> None:
    openai_decision()
    openrouter_decision()


if __name__ == "__main__":
    main()
