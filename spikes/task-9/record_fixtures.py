"""Record raw provider responses for the task-9 adapter tests.

One run makes six small calls (chat and streamed chat on OpenAI and
OpenRouter, and one embeddings call on each). The total cost is below
USD 0.001. The script reads the keys from OPENAI_API_KEY and
OPENROUTER_API_KEY. It never prints or saves them.

The chat calls go through Pydantic AI, so the fixtures hold exactly the
requests that Pydantic AI sends and the responses that it parses.

Run it once from the repository root:

    uv run python spikes/task-9/record_fixtures.py

It writes redacted JSON files to app/tests/fixtures/providers/.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

import httpx2
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.providers.openrouter import OpenRouterProvider

OUT = Path("app/tests/fixtures/providers")
REDACT_KEYS = {"id", "user", "safety_identifier", "billing", "prompt_cache_key"}
ID_RE = re.compile(r'"(id|item_id|response_id)":\s*"[^"]*"')


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if key in REDACT_KEYS:
                if key == "id":
                    out[key] = "redacted"
                continue
            out[key] = _redact(item)
        return out
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _redact_text(text: str) -> str:
    text = ID_RE.sub(lambda m: f'"{m.group(1)}": "redacted"', text)
    return re.sub(r',?\s*"billing":\s*\{[^}]*\}', "", text)


class Recorder(httpx2.AsyncBaseTransport):
    """Pass requests to the real transport and keep the bodies."""

    def __init__(self) -> None:
        self.inner = httpx2.AsyncHTTPTransport()
        self.calls: list[dict[str, Any]] = []

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        response = await self.inner.handle_async_request(request)
        body = await response.aread()
        text = body.decode()
        content_type = response.headers.get("content-type", "")
        entry: dict[str, Any] = {
            "method": request.method,
            "path": request.url.path,
            "request": _redact(json.loads(request.content)),
            "status": response.status_code,
            "content_type": content_type.split(";")[0],
        }
        if "json" in content_type:
            entry["response"] = _redact(json.loads(text))
        else:
            entry["response_text"] = _redact_text(text)
        self.calls.append(entry)
        # aread() decoded the body, so drop the encoding and length headers.
        headers = {
            k: v
            for k, v in response.headers.items()
            if k.lower() not in {"content-encoding", "content-length"}
        }
        return httpx2.Response(response.status_code, headers=headers, content=body)


def _save(name: str, entry: dict[str, Any]) -> None:
    path = OUT / f"{name}.json"
    path.write_text(json.dumps(entry, indent=2) + "\n", encoding="utf-8")
    print("wrote", path, "status", entry["status"])


async def _chat(name: str, model: Any, rec: Recorder, *, stream: bool) -> None:
    agent = Agent(model, instructions="Answer in one word.")
    if stream:
        async with agent.run_stream("Name a primary colour.") as result:
            await result.get_output()
    else:
        await agent.run("Name a primary colour.")
    _save(name, rec.calls[-1])


async def _embed(name: str, base: str, key: str, model: str) -> None:
    rec = Recorder()
    async with httpx2.AsyncClient(transport=rec) as client:
        await client.post(
            f"{base}/embeddings",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": model,
                "input": ["Turn on the heating.", "The light is off."],
                "dimensions": 8,
                "encoding_format": "float",
            },
        )
    _save(name, rec.calls[-1])


async def main() -> None:
    openai_key = os.environ["OPENAI_API_KEY"]
    openrouter_key = os.environ["OPENROUTER_API_KEY"]

    rec = Recorder()
    client = httpx2.AsyncClient(transport=rec)
    openai = OpenAIResponsesModel(
        "gpt-4o-mini",
        provider=OpenAIProvider(api_key=openai_key, http_client=client),
    )
    openrouter = OpenRouterModel(
        "openai/gpt-4o-mini",
        provider=OpenRouterProvider(api_key=openrouter_key, http_client=client),
    )
    await _chat("chat-openai", openai, rec, stream=False)
    await _chat("chat-openai-stream", openai, rec, stream=True)
    await _chat("chat-openrouter", openrouter, rec, stream=False)
    await _chat("chat-openrouter-stream", openrouter, rec, stream=True)
    await client.aclose()

    await _embed(
        "embeddings-openai",
        "https://api.openai.com/v1",
        openai_key,
        "text-embedding-3-small",
    )
    await _embed(
        "embeddings-openrouter",
        "https://openrouter.ai/api/v1",
        openrouter_key,
        "openai/text-embedding-3-small",
    )


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    asyncio.run(main())
