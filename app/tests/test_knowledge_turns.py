"""The knowledge area in real agent turns (task-11 with task-10).

- Each turn includes the store list, the name-sorted index and the injected
  main files, from ``KnowledgeComponent.turn_context`` (design 9.5, 13.4).
- ``memory.search`` runs end to end: the turn history indexes each turn in
  the knowledge search index, and the tool searches it through the executor.
- Query embeddings count towards the agent.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from hagent_app.agents.definition import ModelRef
from hagent_app.app import default_components
from hagent_app.knowledge import KnowledgeComponent
from hagent_app.turns import TurnHistory, TurnInput, TurnRunner, TurnsConfig
from hagent_app.turns.component import TurnsComponent
from helpers.knowledge import MODEL, KnowledgeEnv, knowledge_env
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, FunctionModel

NOW = datetime.fromisoformat("2026-11-15T08:00:00+00:00")


class Chat:
    """A model factory whose model answers "done" and records each request."""

    def __init__(self) -> None:
        self.infos: list[AgentInfo] = []
        self.prompts: list[str] = []

    def __call__(self, ref: ModelRef, agent_id: str) -> Model:
        def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            self.infos.append(info)
            self.prompts.append(str(messages[0].parts[-1].content))  # type: ignore[union-attr]
            return ModelResponse(parts=[TextPart("done")])

        return FunctionModel(respond, model_name="chat")


def _runner(
    k: KnowledgeEnv, history: TurnHistory, chat: Chat, **config: Any
) -> TurnRunner:
    return TurnRunner(
        lambda: k.executor,
        history,
        chat,
        TurnsConfig(**config),
        k.component.turn_context,
    )


def test_default_components_wire_the_knowledge_into_turns() -> None:
    components = {c.name: c for c in default_components()}
    knowledge = components["knowledge"]
    turns = components["turns"]
    assert isinstance(knowledge, KnowledgeComponent)
    assert isinstance(turns, TurnsComponent)
    assert turns.history.index is knowledge.history_index
    assert turns.knowledge_context == knowledge.turn_context
    names = list(components)
    assert names.index("tools") < names.index("knowledge") < names.index("turns")


# ------------------------------------------------------------------ AC 7


async def test_each_turn_includes_stores_index_and_main_files(tmp_path: Path) -> None:
    async with knowledge_env(tmp_path) as k:
        shared = (
            await k.service.create_store(
                "household", "Facts about the occupants.", None
            )
        )["id"]
        for n in range(60):
            await k.service.put_file(shared, f"note-{n:02d}.md", "x", None)
        await k.service.put_file(shared, "MAIN.md", "Shared main, not injected.", None)
        agent = await k.agent("Heater", [{"store_id": shared, "access": "read"}])
        assert agent.private_store is not None
        private = agent.private_store.id
        await k.service.put_file(
            private, "MAIN.md", "Lounge warms in 40 minutes.", None
        )

        history = TurnHistory(k.h.db)
        chat = Chat()
        run = _runner(k, history, chat, main_file_max_chars=30)
        await run.run_turn(agent, TurnInput("invoke", data={"n": 1}, now=NOW))
        await run.run_turn(agent, TurnInput("other", data={"n": 2}, now=NOW))
        await run.drain()

        first, second = (info.instructions or "" for info in chat.infos)
        # The knowledge part is stable across invocations.
        assert first == second
        # Design 9.5 order: instructions, stores, then rules and scopes.
        order = ["# Instructions", "# Knowledge stores", "# Your rules"]
        positions = [first.index(marker) for marker in order]
        assert positions == sorted(positions)
        assert f"- household (id {shared}, read): Facts about the occupants." in first
        assert f"(id {private}, read_write)" in first
        # The index is sorted by name and cut at 50 entries (the default).
        assert "61 entries." in first
        assert "- MAIN.md\n- note-00.md" in first
        assert "- note-48.md" in first and "- note-49.md" not in first
        assert "- ... 11 more (61 entries in total; use knowledge.list)" in first
        # Only the private store injects its main file (the default).
        assert "Lounge warms in 40 minutes." in first
        assert "Shared main, not injected." not in first
        # The main file is near its limit, so the invocation asks to shorten it.
        assert "Maintenance: the main file MAIN.md" in chat.prompts[0]
        assert "Maintenance" not in first


async def test_turn_index_size_comes_from_the_turns_option(tmp_path: Path) -> None:
    async with knowledge_env(tmp_path) as k:
        agent = await k.agent("Few")
        assert agent.private_store is not None
        for name in ("c.md", "a.md", "b.md"):
            await k.service.put_file(agent.private_store.id, name, "x", None)
        chat = Chat()
        run = _runner(k, TurnHistory(k.h.db), chat, index_max_entries=2)
        await run.run_turn(agent, TurnInput("invoke", now=NOW))
        await run.drain()
        text = chat.infos[0].instructions or ""
        assert "- a.md\n- b.md\n- ... 1 more (3 entries in total" in text


async def test_knowledge_off_leaves_the_stores_out(tmp_path: Path) -> None:
    async with knowledge_env(tmp_path) as k:
        agent = await k.agent("Plain", tools__knowledge=False)
        chat = Chat()
        run = _runner(k, TurnHistory(k.h.db), chat)
        await run.run_turn(agent, TurnInput("invoke", now=NOW))
        await run.drain()
        assert "# Knowledge stores" not in (chat.infos[0].instructions or "")


# ------------------------------------------------- memory.search end to end


async def test_memory_search_finds_indexed_turns_through_the_executor(
    tmp_path: Path,
) -> None:
    history = TurnHistory()
    async with knowledge_env(tmp_path, model=MODEL, history=history) as k:
        history.bind(k.h.db)
        history.index = k.component.history_index
        a = await k.agent("Alpha")
        b = await k.agent("Beta")
        run = _runner(k, history, Chat())
        mine = await run.run_turn(
            a, TurnInput("automation", data={"text": "the heater is broken"}, now=NOW)
        )
        await run.run_turn(
            a,
            TurnInput(
                "assist",
                conversation_id="c1",
                data={"text": "close the blinds"},
                now=NOW,
            ),
        )
        await run.run_turn(
            b, TurnInput("automation", data={"text": "boiler of beta"}, now=NOW)
        )
        await run.run_turn(
            a, TurnInput("test", data={"text": "heater dry run"}, dry_run=True, now=NOW)
        )
        await run.drain()
        await k.idle()
        indexed = await k.rows(
            "SELECT collection, agent_id, source_id FROM search_passages "
            "WHERE collection IN ('history', 'conversation') ORDER BY id"
        )
        assert [(r[0], r[1]) for r in indexed] == [
            ("history", a.id),
            ("conversation", a.id),
            ("history", b.id),
        ]

        k.embeddings.calls.clear()
        result = await k.call(a, "memory.search", {"query": "boiler"})
        assert result.ok, result
        # The heater turn has no keyword match; the vector search finds it
        # first. Beta's turn never appears: the search sees Alpha's only.
        assert result.data[0]["turn_id"] == mine.turn_id
        assert result.data[0]["matched"] == ["vector"]
        assert result.data[0]["source"] == "automation"
        own = {r[2] for r in indexed[:2]}
        assert {r["turn_id"] for r in result.data} <= own
        # The query embedding counts towards the invocation agent.
        assert k.embeddings.calls == [(MODEL.model, ("boiler",), a.id)]

        blinds = await k.call(a, "memory.search", {"query": "blinds"})
        assert blinds.ok
        assert blinds.data[0]["collection"] == "conversation"
        assert blinds.data[0]["matched"] == ["keyword", "vector"]
        filtered = await k.call(
            a, "memory.search", {"query": "blinds", "source": "automation"}
        )
        assert filtered.ok
        assert all(r["source"] == "automation" for r in filtered.data)
        assert all(r["collection"] == "history" for r in filtered.data)


async def test_knowledge_search_charges_the_query_to_the_agent(tmp_path: Path) -> None:
    async with knowledge_env(tmp_path, model=MODEL) as k:
        agent = await k.agent("Payer")
        assert agent.private_store is not None
        await k.service.put_file(agent.private_store.id, "a.md", "boiler notes", None)
        await k.idle()
        k.embeddings.calls.clear()
        result = await k.call(agent, "knowledge.search", {"query": "boiler pressure"})
        assert result.ok
        assert k.embeddings.calls == [(MODEL.model, ("boiler pressure",), agent.id)]
        # A search from the management API counts towards the installation.
        k.embeddings.calls.clear()
        status, _ = await k.request(
            "GET", "/api/knowledge/search", params={"q": "pump pressure"}
        )
        assert status == 200
        assert k.embeddings.calls == [(MODEL.model, ("pump pressure",), None)]
