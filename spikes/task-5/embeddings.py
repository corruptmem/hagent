"""Probe: one embeddings request on each provider.

Both providers take the OpenAI embeddings shape, so one client class serves
both. Only the base URL, the key and the model name change:

- OpenAI: ``https://api.openai.com/v1``, model ``text-embedding-3-small``.
- OpenRouter: ``https://openrouter.ai/api/v1``, model
  ``openai/text-embedding-3-small``.

The script sends a batch of two texts, and the ``dimensions`` parameter, to
check that both providers accept it. It writes a short summary of each
response (not the full vectors) to ``samples/``.

Run: ``uv run python spikes/task-5/embeddings.py``
"""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path
from typing import Any

from openai import OpenAI

SAMPLES = Path(__file__).parent / "samples"
TEXTS = ["Turn on the heating in the living room.", "The kitchen light is off."]
DIMENSIONS = 256

PROVIDERS = [
    ("openai", "https://api.openai.com/v1", "OPENAI_API_KEY", "text-embedding-3-small"),
    (
        "openrouter",
        "https://openrouter.ai/api/v1",
        "OPENROUTER_API_KEY",
        "openai/text-embedding-3-small",
    ),
]


def probe(name: str, base_url: str, key_env: str, model: str) -> bool:
    key = os.environ.get(key_env)
    if not key:
        print(f"{name}: {key_env} is not set, skipped")
        return False
    client = OpenAI(base_url=base_url, api_key=key)
    raw = client.embeddings.with_raw_response.create(
        model=model,
        input=TEXTS,
        dimensions=DIMENSIONS,
        encoding_format="float",
    )
    body: dict[str, Any] = json.loads(raw.text)
    vectors = [item["embedding"] for item in body["data"]]
    summary = {
        "base_url": base_url,
        "request": {"model": model, "input": TEXTS, "dimensions": DIMENSIONS},
        "response_keys": sorted(body),
        "model": body.get("model"),
        "count": len(vectors),
        "dims": [len(v) for v in vectors],
        "norms": [round(math.sqrt(sum(x * x for x in v)), 4) for v in vectors],
        "first_values": [v[:3] for v in vectors],
        "usage": body.get("usage"),
        "extra": {k: body[k] for k in body if k not in {"data", "model", "usage"}},
    }
    SAMPLES.mkdir(exist_ok=True)
    out = SAMPLES / f"embeddings-{name}.json"
    out.write_text(json.dumps(summary, indent=2) + "\n")
    print(f"{name}: written to {out.name}")
    print(json.dumps(summary, indent=2))
    return len(vectors) == len(TEXTS) and all(len(v) == DIMENSIONS for v in vectors)


def main() -> int:
    results = [probe(*p) for p in PROVIDERS]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
