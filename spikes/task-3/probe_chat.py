"""Probe chat usage and cost fields, with a repeated prefix to hit the cache.

Usage: probe_chat.py [all|openrouter|openai] [model ...]

Each model gets three requests per provider. The prefix is about 2,500
tokens, above the 1,024-token minimum for OpenAI prompt caching. OpenAI
caching is best effort: the requests send a `prompt_cache_key` and wait 6 s
between calls. Spend per run with the default models: under USD 0.005.
"""

import sys
import time

from _http import call, dump

PREFIX = "Reference list of colours, ignore it unless asked.\n" + "\n".join(
    f"Item {i}: the colour code for slot {i} is C{i * 7919 % 10007}."
    for i in range(160)
)
QUESTION = "Reply with the single word: ok"
DEFAULT_MODELS = ("openai/gpt-4o-mini", "openai/gpt-6-luna")
ATTEMPTS = (1, 2, 3)
PAUSE_S = 6


def _reasoning(model: str) -> dict[str, str] | None:
    bare = model.removeprefix("openai/")
    return {"effort": "minimal"} if bare.startswith(("gpt-5", "gpt-6")) else None


def openrouter(model: str) -> None:
    gen_id = ""
    for attempt in ATTEMPTS:
        body: dict[str, object] = {
            "model": model,
            "messages": [
                {"role": "system", "content": PREFIX},
                {"role": "user", "content": QUESTION},
            ],
            "max_tokens": 200,
            "usage": {"include": True},
        }
        if reasoning := _reasoning(model):
            body["reasoning"] = reasoning
        resp, _ = call("openrouter", "/chat/completions", body)
        dump(f"openrouter chat {model} attempt {attempt}", resp.get("usage", resp))
        gen_id = resp.get("id", "")
        time.sleep(PAUSE_S)
    # The generation record appears a few seconds after the call.
    gen, _ = call("openrouter", f"/generation?id={gen_id}")
    data = gen.get("data", gen)
    keys = (
        "total_cost",
        "usage",
        "cache_discount",
        "upstream_inference_cost",
        "tokens_prompt",
        "native_tokens_prompt",
        "native_tokens_cached",
        "http_error",
    )
    dump("openrouter /generation (last attempt)", {k: data.get(k) for k in keys})


def openai_responses(model: str) -> None:
    bare = model.removeprefix("openai/")
    for attempt in ATTEMPTS:
        body: dict[str, object] = {
            "model": bare,
            "instructions": PREFIX,
            "input": QUESTION,
            "max_output_tokens": 200,
            "store": False,
            "prompt_cache_key": "hagent-task3-probe",
        }
        if reasoning := _reasoning(bare):
            body["reasoning"] = reasoning
        resp, headers = call("openai", "/responses", body)
        dump(f"openai responses {bare} attempt {attempt}", resp.get("usage", resp))
        cost_keys = {k: v for k, v in resp.items() if "cost" in k or "billing" in k}
        dump("cost-like top-level keys", cost_keys)
        cost_headers = [
            k for k in headers if "cost" in k.lower() or "price" in k.lower()
        ]
        dump("cost-like headers", cost_headers)
        time.sleep(PAUSE_S)


def main() -> None:
    target = sys.argv[1] if len(sys.argv) > 1 else "all"
    models = sys.argv[2:] or list(DEFAULT_MODELS)
    for model in models:
        if target in {"all", "openrouter"}:
            openrouter(model)
        if target in {"all", "openai"}:
            openai_responses(model)


if __name__ == "__main__":
    main()
