"""A fake provider API for the providers tests.

``FakeProviders`` is an ``httpx2.MockTransport`` handler. It answers each
request with a recorded fixture from ``tests/fixtures/providers/``. It keeps
every request, so that tests can check URLs, headers and bodies.

The fixtures are real responses, recorded on 2026-11-14 with redacted IDs:

- ``chat-*.json`` and ``embeddings-*.json``: ``spikes/task-9/record_fixtures.py``.
- ``decision-*.json``: copied from ``spikes/task-5/samples/``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2
from hagent_app.providers import ChoiceQuestion, Level, ScoreQuestion, YesNoQuestion

FIXTURES = Path(__file__).parent.parent / "fixtures" / "providers"

OPENAI_BASE = "https://api.openai.test/v1"
OPENROUTER_BASE = "https://openrouter.test/api/v1"
BASE_URLS = {"openai": OPENAI_BASE, "openrouter": OPENROUTER_BASE}


def fixture(name: str) -> dict[str, Any]:
    """Load ``tests/fixtures/providers/<name>.json``."""
    data: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text())
    return data


@dataclass
class Seen:
    """One request that the fake received."""

    method: str
    url: str
    headers: dict[str, str]
    body: Any


@dataclass
class Reply:
    """A response. ``fixture`` names a fixture file. ``gate`` delays it."""

    fixture: str | None = None
    status: int = 200
    json_body: Any = None
    text: str | None = None
    gate: asyncio.Event | None = None


@dataclass
class FakeProviders:
    """Routes ``(host part, path)`` to replies, and records requests."""

    routes: dict[str, list[Reply]] = field(default_factory=dict)
    seen: list[Seen] = field(default_factory=list)

    def reply(self, path: str, *replies: Reply) -> None:
        """Queue replies for ``path``. The last reply repeats."""
        self.routes[path] = list(replies)

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self._handle)

    def client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=self.transport())

    async def _handle(self, request: httpx2.Request) -> httpx2.Response:
        body: Any = None
        if request.content:
            body = json.loads(request.content)
        self.seen.append(
            Seen(request.method, str(request.url), dict(request.headers), body)
        )
        key = f"{request.url.host}{request.url.path}"
        replies = self.routes.get(key)
        if not replies:
            return httpx2.Response(404, json={"error": f"no route {key}"})
        reply = replies.pop(0) if len(replies) > 1 else replies[0]
        if reply.gate is not None:
            await reply.gate.wait()
        if reply.fixture is not None:
            data = fixture(reply.fixture)
            if "response" in data:
                return httpx2.Response(reply.status, json=data["response"])
            return httpx2.Response(
                reply.status,
                content=data["response_text"].encode(),
                headers={"content-type": "text/event-stream"},
            )
        if reply.text is not None:
            return httpx2.Response(reply.status, text=reply.text)
        return httpx2.Response(reply.status, json=reply.json_body)


def route(provider: str, path: str) -> str:
    """The route key for ``path`` below the base URL of ``provider``."""
    url = httpx2.URL(BASE_URLS[provider] + path)
    return f"{url.host}{url.path}"


def openrouter_alpha(path: str) -> str:
    """The route key for an OpenRouter ``/api/alpha`` path."""
    return f"openrouter.test/api/alpha{path}"


OPENAI_KEY = "sk-test-openai-SENTINEL-0001"
OPENROUTER_KEY = "sk-or-test-SENTINEL-0002"


def provider_options(**extra: Any) -> dict[str, Any]:
    """A ``providers`` options section with test keys and fake base URLs."""
    options: dict[str, Any] = {
        "openai_api_key": OPENAI_KEY,
        "openrouter_api_key": OPENROUTER_KEY,
        "base_urls": dict(BASE_URLS),
        "refresh_openrouter_prices": False,
    }
    options.update(extra)
    return options


# The values and questions of the recorded decision requests.
VALUES = {
    "sensor": "living room temperature",
    "value_c": 17.2,
    "target_c": 21.0,
    "occupied": True,
    "window_open": False,
    "time": "07:10",
}
QUESTIONS = (
    YesNoQuestion(
        "should_heat",
        "Should the heating turn on now?",
        yes="The room is occupied, below target and no window is open.",
        no="Heating would waste energy or is not needed.",
    ),
    ChoiceQuestion(
        "mode",
        "Which heating mode fits best?",
        {
            "comfort": "Occupied room, well below target.",
            "eco": "Small gap to target, or nobody is home.",
            "off": "No heating needed.",
        },
    ),
    ScoreQuestion(
        "urgency",
        "How urgent is heating?",
        [
            Level("low", "At or near target."),
            Level("medium", "A few degrees below target."),
            Level("high", "Far below target and occupied."),
        ],
    ),
)
