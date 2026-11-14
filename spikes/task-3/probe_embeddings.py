"""Probe embedding usage and cost fields on both providers. Cost: negligible."""

import time

from _http import call, dump

TEXTS = ["The kitchen light turns on at sunset.", "Heating schedule for weekdays."]


def main() -> None:
    body = {"model": "openai/text-embedding-3-small", "input": TEXTS}
    resp, _ = call("openrouter", "/embeddings", body)
    dump("openrouter embeddings usage", resp.get("usage", resp))
    dump("openrouter embeddings top-level keys", sorted(resp.keys()))
    gen_id = resp.get("id")
    if gen_id:
        time.sleep(5)
        gen, _ = call("openrouter", f"/generation?id={gen_id}")
        data = gen.get("data", gen)
        keys = ("total_cost", "usage", "native_tokens_prompt", "api_type")
        dump("openrouter /generation for embedding", {k: data.get(k) for k in keys})

    body = {"model": "text-embedding-3-small", "input": TEXTS}
    resp, headers = call("openai", "/embeddings", body)
    dump("openai embeddings usage", resp.get("usage", resp))
    dump("openai embeddings top-level keys", sorted(resp.keys()))
    dump("openai cost-like headers", [k for k in headers if "cost" in k.lower()])


if __name__ == "__main__":
    main()
