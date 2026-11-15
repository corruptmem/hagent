"""Test helpers for the knowledge area: a fake embedding model and a started
``KnowledgeComponent`` with a real tool executor."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from aiohttp.test_utils import TestClient, TestServer
from hagent_app.agents import Agent
from hagent_app.agents.definition import AgentDefinition
from hagent_app.app import AppContext
from hagent_app.config import Config
from hagent_app.http import ChangeEvent, ChangeEvents, HttpServer
from hagent_app.knowledge import KnowledgeComponent
from hagent_app.knowledge.embedder import EmbeddingModel
from hagent_app.providers.config import Provider
from hagent_app.providers.embeddings import EmbeddingResult
from hagent_app.providers.prices import TokenUsage
from hagent_app.tools import AuditLog, ToolContext, ToolExecutor, ToolResult
from hagent_app.tools.base import ToolEnv

from helpers.tools import Harness, harness
from helpers.wait import wait_until

DIMS = 32
# Words in one group get the same vector direction: a toy "meaning".
SYNONYMS = [
    {"boiler", "furnace", "heater"},
    {"warm", "heat", "hot", "heating"},
    {"cold", "chilly", "freezing"},
    {"radiator", "rad"},
    {"lamp", "light", "bulb"},
    {"bedroom", "sleeping"},
]
_CANON = {w: sorted(group)[0] for group in SYNONYMS for w in group}
_WORD = re.compile(r"[a-z]+")


def fake_vector(text: str) -> list[float]:
    """A unit vector from the canonical words of ``text``."""
    vector = [0.0] * DIMS
    for word in _WORD.findall(text.lower()):
        canon = _CANON.get(word, word)
        digest = hashlib.sha256(canon.encode()).digest()
        vector[digest[0] % DIMS] += 1.0
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norm for v in vector]


@dataclass
class FakeEmbeddings:
    """An ``EmbeddingClient`` that records each call."""

    calls: list[tuple[str, tuple[str, ...], str | None]] = field(default_factory=list)
    fail: bool = False

    async def embed(
        self,
        provider: Provider,
        model: str,
        texts: Sequence[str],
        *,
        agent_id: str | None,
        dimensions: int | None = None,
    ) -> EmbeddingResult:
        if self.fail:
            msg = "the fake provider is down"
            raise RuntimeError(msg)
        self.calls.append((model, tuple(texts), agent_id))
        return EmbeddingResult(model, [fake_vector(t) for t in texts], TokenUsage())

    def embedded_texts(self) -> list[str]:
        """Every text of every call, queries included."""
        return [t for _, texts, _ in self.calls for t in texts]


class FakeTools:
    """The part of ``ToolsComponent`` that the knowledge component reads."""

    def __init__(self, h: Harness) -> None:
        self.registry = h.registry


@dataclass
class KnowledgeEnv:
    """A started knowledge component, a tool executor and an API client."""

    h: Harness
    component: KnowledgeComponent
    executor: ToolExecutor
    api: TestClient[Any, Any]
    embeddings: FakeEmbeddings
    events: list[ChangeEvent]

    @property
    def service(self) -> Any:
        return self.component.service()

    async def idle(self) -> None:
        """Wait until the embedder has no work."""
        embedder = self.component.embedder
        assert embedder is not None
        await embedder.wait_idle()

    async def agent(
        self, name: str, knowledge: list[dict[str, Any]] | None = None, **changes: Any
    ) -> Agent:
        """Create an agent. ``knowledge`` adds attachments to the private store."""
        agent = await self.h.agent(name=name, **changes)
        if knowledge:
            data = agent.definition.to_data()
            data["knowledge"] = [*data["knowledge"], *knowledge]
            agent = await self.h.agents.update(
                agent.id, AgentDefinition.from_json(json.dumps(data))
            )
        return agent

    async def call(
        self, agent: Agent, tool: str, arguments: Mapping[str, Any]
    ) -> ToolResult:
        ctx = ToolContext(agent, invocation_id="inv-k")
        return await self.executor.execute(ctx, tool, arguments)

    async def request(
        self, method: str, path: str, body: Any = None, **kwargs: Any
    ) -> tuple[int, Any]:
        if body is not None:
            kwargs["json"] = body
        async with self.api.request(method, path, **kwargs) as resp:
            return resp.status, await resp.json()

    async def rows(self, sql: str, *params: Any) -> list[Any]:
        return await self.h.db.run(lambda c: c.execute(sql, params).fetchall())


@asynccontextmanager
async def knowledge_env(
    tmp_path: Path,
    *,
    options: Mapping[str, Any] | None = None,
    model: EmbeddingModel | None = None,
    history: Any = None,
) -> AsyncIterator[KnowledgeEnv]:
    """Start the knowledge component on the tool harness database."""
    async with harness(tmp_path) as h:
        events = ChangeEvents()
        seen: list[ChangeEvent] = []
        events.subscribe(seen.append)
        fake = FakeEmbeddings()
        component = KnowledgeComponent(
            events, tools=cast(Any, FakeTools(h)), embedding_client=fake
        )
        config = Config(
            ha_url=h.ha.url,
            ha_token=h.ha.token,
            data_dir=tmp_path,
            options={"knowledge": dict(options or {"embed_retry_seconds": 0.05})},
        )
        ctx = AppContext(config, h.db, h.client, h.states, "test-installation")
        await component.start(ctx)
        if model is not None:
            await component.set_embedding_model(model)
        env = ToolEnv(
            ha=h.client,
            states=h.states,
            registry=h.registry,
            knowledge=component.tools_store,
            history=history if history is not None else h.history,
            entities=component.entity_search,
        )
        executor = ToolExecutor(env, AuditLog(h.db))
        http = HttpServer()
        http.add_routes(component.routes())
        client: TestClient[Any, Any] = TestClient(TestServer(http.make_app()))
        await client.start_server()
        try:
            await wait_until(lambda: component.catalog is not None)
            yield KnowledgeEnv(h, component, executor, client, fake, seen)
        finally:
            await client.close()
            await component.stop()


MODEL = EmbeddingModel("openai", "text-embedding-3-small", 32)
