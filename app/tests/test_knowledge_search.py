"""Hybrid search, embeddings, fallback and the turn context (design 13.3, 13.4)."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from hagent_app.knowledge import HistoryIndex, build_turn_context, vectors
from hagent_app.knowledge.embedder import EmbeddingModel
from hagent_app.knowledge.entities import entity_texts
from hagent_app.knowledge.index import Scope
from helpers.knowledge import MODEL, KnowledgeEnv, knowledge_env
from helpers.wait import wait_until

MODEL_2 = EmbeddingModel("openrouter", "openai/text-embedding-3-large", 32)


async def _store(k: KnowledgeEnv, name: str, description: str = "") -> str:
    created = await k.service.create_store(name, description, None)
    return str(created["id"])


class HistoryAdapter:
    """A ``HistoryStore`` whose search uses the history index (task-10 wires the
    real one)."""

    def __init__(self) -> None:
        self.index: HistoryIndex | None = None
        self.calls: list[str] = []

    async def search(
        self, agent_id: str, query: str, filters: Mapping[str, Any], limit: int
    ) -> list[dict[str, Any]]:
        self.calls.append(agent_id)
        assert self.index is not None
        return await self.index.search(agent_id, query, filters, limit)

    async def rule_owner(self, rule_id: str) -> str | None:
        return None

    async def rule_stats(self, agent_id: str, rule_id: str) -> dict[str, Any]:
        return {}


# ------------------------------------------------------------------ AC 4


async def test_knowledge_search_is_hybrid_and_limited_to_attached_stores(
    tmp_path: Path,
) -> None:
    async with knowledge_env(tmp_path, model=MODEL) as k:
        mine = await _store(k, "mine")
        other = await _store(k, "other")
        await k.service.put_file(
            mine, "service.md", "The furnace gets a service in May.", None
        )
        await k.service.put_file(
            mine, "pump.md", "The pump needs a boiler check.", None
        )
        await k.service.put_file(other, "x.md", "boiler boiler boiler service", None)
        await k.idle()
        agent = await k.agent("Seeker", [{"store_id": mine}])

        result = await k.call(agent, "knowledge.search", {"query": "boiler"})
        assert result.ok, result
        paths = [r["path"] for r in result.data]
        assert set(paths) == {"service.md", "pump.md"}
        assert all(r["store_id"] == mine for r in result.data)
        # The furnace passage has no keyword match; the vector search finds it.
        furnace = next(r for r in result.data if r["path"] == "service.md")
        assert furnace["matched"] == ["vector"]
        assert "search_mode" not in furnace
        direct = await k.service.search_stores([mine], "boiler", 5)
        assert direct.mode == "hybrid"

        # Naming a store that is not attached fails before any store or
        # provider call.
        before = len(k.embeddings.calls)
        denied = await k.call(
            agent, "knowledge.search", {"query": "boiler", "store_ids": [other]}
        )
        assert denied.error_code == "store_not_attached"
        assert len(k.embeddings.calls) == before


async def test_knowledge_tools_out_of_scope_never_reach_the_store(
    tmp_path: Path,
) -> None:
    async with knowledge_env(tmp_path) as k:
        sid = await _store(k, "guarded")
        await k.service.put_file(sid, "a.md", "secret", None)
        reader = await k.agent("Reader", [{"store_id": sid, "access": "read"}])
        stranger = await k.agent("Stranger")
        before = await k.rows("SELECT * FROM knowledge_item_versions")
        calls: list[tuple[Any, str, dict[str, Any]]] = [
            (stranger, "knowledge.read", {"store_id": sid, "path": "a.md"}),
            (stranger, "knowledge.list", {"store_id": sid}),
            (stranger, "knowledge.search", {"query": "secret", "store_ids": [sid]}),
            (
                reader,
                "knowledge.write",
                {"store_id": sid, "path": "a.md", "mode": "append", "content": "x"},
            ),
            (reader, "knowledge.delete", {"store_id": sid, "path": "a.md"}),
        ]
        for agent, tool, args in calls:
            result = await k.call(agent, tool, args)
            assert result.error_code in {"store_not_attached", "read_only"}, tool
        assert await k.rows("SELECT * FROM knowledge_item_versions") == before
        # The stranger's own search sees only its private store.
        own = await k.call(stranger, "knowledge.search", {"query": "secret"})
        assert own.ok and own.data == []


async def test_memory_search_is_hybrid_and_limited_to_the_agent(
    tmp_path: Path,
) -> None:
    adapter = HistoryAdapter()
    async with knowledge_env(tmp_path, model=MODEL, history=adapter) as k:
        history = k.component.history
        assert history is not None
        adapter.index = history
        a = await k.agent("Alpha")
        b = await k.agent("Beta")
        await history.add(
            a.id,
            "r1",
            "Turned the heater on at 06:00.",
            source="automation.morning",
            record_type="invocation",
            at="2026-11-10T06:00:00+00:00",
        )
        await history.add(
            a.id,
            "r2",
            "Closed the blinds.",
            source="assist",
            record_type="invocation",
            at="2026-11-12T06:00:00+00:00",
        )
        await history.add(
            a.id,
            "c1",
            "Is the boiler on?",
            collection="conversation",
            source="assist",
            record_type="message",
            at="2026-11-12T07:00:00+00:00",
        )
        await history.add(
            b.id,
            "r9",
            "Beta turned the boiler off.",
            source="automation.morning",
            record_type="invocation",
            at="2026-11-10T06:00:00+00:00",
        )
        await k.idle()

        result = await k.call(a, "memory.search", {"query": "boiler"})
        assert result.ok, result
        ids = [r["record_id"] for r in result.data]
        # r1 has no keyword match; the vector search finds it (heater ~ boiler).
        # r2 is unrelated: the distance cutoff keeps it out. r9 is Beta's.
        assert set(ids) == {"c1", "r1"}
        assert adapter.calls == [a.id]
        filtered = await k.call(
            a,
            "memory.search",
            {
                "query": "boiler",
                "source": "automation.morning",
                "since": "2026-11-01T00:00:00+00:00",
            },
        )
        assert [r["record_id"] for r in filtered.data] == ["r1"]
        assert filtered.data[0]["matched"] == ["vector"]
        # The agent ID comes from the invocation; an argument cannot change it.
        spoof = await k.call(a, "memory.search", {"query": "boiler", "agent_id": b.id})
        assert spoof.error_code == "invalid_arguments"
        # Retention removes a record from the index.
        await history.remove(a.id, "r1")
        result = await k.call(a, "memory.search", {"query": "heater"})
        assert all(r["record_id"] != "r1" for r in result.data)


async def test_find_entities_ranks_only_entities_in_scope(tmp_path: Path) -> None:
    async with knowledge_env(tmp_path, model=MODEL) as k:
        catalog = k.component.catalog
        assert catalog is not None
        await catalog.rebuild(k.h.registry.current())
        await k.idle()
        texts = entity_texts(k.h.registry.current(), k.h.states)
        assert "area: Kitchen" in texts["light.kitchen"][1]
        assert "labels: heating" in texts["climate.lounge"][1]

        in_scope = ["light.kitchen", "light.hall", "lock.front"]
        scope = {"level": "read", "scope": {"entities": in_scope}}
        agent = await k.agent("Finder", tools__ha=scope)
        result = await k.call(agent, "ha.find_entities", {"query": "kitchen lamp"})
        assert result.ok, result
        found = [r["entity_id"] for r in result.data]
        # light.kitchen_spot and switch.kettle (kitchen) rank high, but they
        # are outside the scope.
        assert found[0] == "light.kitchen"
        # lock.front is in scope but unrelated: the distance cutoff drops it.
        assert set(found) == {"light.kitchen", "light.hall"}

        # No word of the query is in the text; the vector search finds it.
        result = await k.call(agent, "ha.find_entities", {"query": "bulb"})
        bulb = {r["entity_id"] for r in result.data}
        assert bulb == {"light.kitchen", "light.hall"}

        # The ranker gets only the scope-filtered candidates.
        ranked = await catalog.rank_entities("kitchen", ["light.hall"], 10)
        assert set(ranked) <= {"light.hall"}


async def test_find_entities_uses_the_word_match_without_a_catalogue(
    tmp_path: Path,
) -> None:
    async with knowledge_env(tmp_path) as k:
        catalog = k.component.catalog
        assert catalog is not None
        catalog.size = 0
        scope = {"level": "read", "scope": {"areas": ["kitchen"]}}
        agent = await k.agent("Finder", tools__ha=scope)
        result = await k.call(agent, "ha.find_entities", {"query": "kettle"})
        assert [r["entity_id"] for r in result.data] == ["switch.kettle"]
        result = await k.call(agent, "ha.find_entities", {"query": "hall"})
        assert result.data == []


# ------------------------------------------------------------------ AC 5


async def test_embeddings_are_queued_deduplicated_and_charged(tmp_path: Path) -> None:
    async with knowledge_env(tmp_path, model=MODEL) as k:
        sid = await _store(k, "dedup")
        agent = await k.agent("Writer", [{"store_id": sid, "access": "read_write"}])
        for path in ("a.md", "b.md"):
            result = await k.call(
                agent,
                "knowledge.write",
                {
                    "store_id": sid,
                    "path": path,
                    "mode": "create",
                    "content": "Same fact about the radiator.",
                },
            )
            assert result.ok
        await k.service.upload(
            sid, "doc.txt", b"A document line.", "text/plain", replace=False
        )
        await k.idle()
        texts = k.embeddings.embedded_texts()
        assert texts.count("Same fact about the radiator.") == 1
        charges = {
            t: agent_id for _, batch, agent_id in k.embeddings.calls for t in batch
        }
        assert charges["Same fact about the radiator."] == agent.id
        assert charges["A document line."] is None
        rows = await k.rows("SELECT DISTINCT model FROM search_embeddings")
        assert [r[0] for r in rows] == [MODEL.key]
        assert await k.component.embedder.pending() == 0  # type: ignore[union-attr]

        # The entity catalogue (built at start) is charged to the installation.
        entity_charges = {
            agent_id
            for _, batch, agent_id in k.embeddings.calls
            if any(t.startswith("light.hall") for t in batch)
        }
        assert entity_charges == {None}

        # A model change re-indexes every passage with the new model.
        k.embeddings.calls.clear()
        status, body = await k.request(
            "PUT", "/api/knowledge/settings", {"embedding": MODEL_2.to_data()}
        )
        assert status == 200 and body["embedding"]["provider"] == "openrouter"
        await k.idle()
        models = {model for model, _, _ in k.embeddings.calls}
        assert models == {MODEL_2.model}
        rows = await k.rows("SELECT DISTINCT model FROM search_embeddings")
        assert [r[0] for r in rows] == [MODEL_2.key]
        distinct = await k.rows(
            "SELECT COUNT(DISTINCT content_hash) FROM search_passages"
        )
        vectors_count = await k.rows("SELECT COUNT(*) FROM search_embeddings")
        assert vectors_count[0][0] == distinct[0][0]
        status, body = await k.request("GET", "/api/knowledge/status")
        assert body["search_mode"] == "hybrid" and body["pending_embeddings"] == 0

        # Deleting text removes vectors that nothing uses.
        await k.service.delete_user_item(sid, "doc.txt", None)
        k.component.embedder.wake()  # type: ignore[union-attr]
        await k.idle()
        left = await k.rows(
            "SELECT COUNT(*) FROM search_embeddings WHERE content_hash NOT IN "
            "(SELECT content_hash FROM search_passages)"
        )
        assert left[0][0] == 0


async def test_model_setting_survives_a_restart(tmp_path: Path) -> None:
    async with knowledge_env(tmp_path, model=MODEL_2) as k:
        pass
    async with knowledge_env(tmp_path) as k:
        embedder = k.component.embedder
        assert embedder is not None and embedder.model == MODEL_2
        status, body = await k.request(
            "PUT", "/api/knowledge/settings", {"embedding": None}
        )
        assert status == 200 and body == {"embedding": None}
        status, body = await k.request(
            "PUT",
            "/api/knowledge/settings",
            {"embedding": {"provider": "nope", "model": "x"}},
        )
        assert status == 400


# ------------------------------------------------------------------ AC 6


async def test_keyword_only_without_a_model(tmp_path: Path) -> None:
    async with knowledge_env(tmp_path) as k:
        sid = await _store(k, "plain")
        await k.service.put_file(
            sid, "a.md", "The furnace is old.\n\nThe boiler is new.", None
        )
        result = await k.service.search_stores([sid], "boiler", 5)
        assert result.mode == "keyword"
        assert result.fallback_reason == "no embedding model is set"
        assert [h.matched for h in result.hits] == [("keyword",)]
        status, body = await k.request(
            "GET", "/api/knowledge/search", params={"q": "boiler"}
        )
        assert status == 200 and body["mode"] == "keyword"
        assert len(body["results"]) == 1
        agent = await k.agent("A", [{"store_id": sid}])
        tool = await k.call(agent, "knowledge.search", {"query": "boiler"})
        assert tool.ok and tool.data[0]["search_mode"] == "keyword"
        assert k.embeddings.calls == []


async def test_keyword_only_when_sqlite_vec_is_off(tmp_path: Path) -> None:
    options = {"load_sqlite_vec": False}
    async with knowledge_env(tmp_path, options=options, model=MODEL) as k:
        sid = await _store(k, "plain")
        await k.service.put_file(sid, "a.md", "The boiler is new.", None)
        result = await k.service.search_stores([sid], "boiler", 5)
        assert result.mode == "keyword"
        assert "sqlite-vec" in (result.fallback_reason or "")
        assert len(result.hits) == 1
        _, body = await k.request("GET", "/api/knowledge/status")
        assert body["sqlite_vec"] is False and body["search_mode"] == "keyword"
        assert k.embeddings.calls == []


async def test_keyword_only_when_the_provider_fails(tmp_path: Path) -> None:
    async with knowledge_env(tmp_path, model=MODEL) as k:
        k.embeddings.fail = True
        sid = await _store(k, "plain")
        await k.service.put_file(sid, "a.md", "The boiler is new.", None)
        await k.idle()
        result = await k.service.search_stores([sid], "boiler", 5)
        assert result.mode == "keyword"
        assert "provider failed" in (result.fallback_reason or "")
        assert len(result.hits) == 1
        _, body = await k.request("GET", "/api/knowledge/status")
        assert body["search_mode"] == "keyword"
        assert "fake provider is down" in body["last_error"]
        embedder = k.component.embedder
        assert embedder is not None
        assert await embedder.pending() > 0
        assert await k.rows("SELECT * FROM search_embeddings") == []
        # The provider comes back: the embedder retries on its own (after
        # embed_retry_seconds), and the queue empties.
        distinct = await k.rows(
            "SELECT COUNT(DISTINCT content_hash) FROM search_passages"
        )
        k.embeddings.fail = False
        await wait_until(lambda: embedder.embedded_texts >= distinct[0][0])
        assert await embedder.pending() == 0
        stored = await k.rows("SELECT COUNT(*) FROM search_embeddings")
        assert stored[0][0] == distinct[0][0] > 0
        result = await k.service.search_stores([sid], "boiler", 5)
        assert result.mode == "hybrid"
        assert result.hits[0].matched == ("keyword", "vector")


def test_load_failure_gives_a_reason() -> None:
    class NoExtensions(sqlite3.Connection):
        def enable_load_extension(self, enabled: bool, /) -> None:
            msg = "not built with extension support"
            raise AttributeError(msg)

    conn = sqlite3.connect(":memory:", factory=NoExtensions)
    try:
        reason = vectors.load(conn)
    finally:
        conn.close()
    assert reason is not None and "extension support" in reason


def test_sqlite_vec_loads_here() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        assert vectors.load(conn) is None
    finally:
        conn.close()


@pytest.mark.parametrize("bad", ['"', "AND OR NOT", "x*:-", "NEAR(a b)"])
async def test_query_syntax_never_breaks_search(tmp_path: Path, bad: str) -> None:
    async with knowledge_env(tmp_path) as k:
        sid = await _store(k, "plain")
        await k.service.put_file(sid, "a.md", "and or not near", None)
        index = k.component.index
        assert index is not None
        result = await index.search(Scope(("knowledge",), store_ids=(sid,)), bad, 5)
        assert result.mode == "keyword"


# ------------------------------------------------------------------ AC 7


async def test_turn_context_lists_stores_index_and_main_files(tmp_path: Path) -> None:
    async with knowledge_env(tmp_path) as k:
        shared = await _store(k, "household", "Facts about the occupants.")
        for n in range(60):
            await k.service.put_file(shared, f"note-{n:02d}.md", "x", None)
        await k.service.put_file(shared, "MAIN.md", "Shared main, not injected.", None)
        agent = await k.agent("Heater", [{"store_id": shared, "access": "read"}])
        assert agent.private_store is not None
        private = agent.private_store.id
        await k.service.put_file(
            private, "MAIN.md", "Lounge warms in 40 minutes.", None
        )
        await k.service.put_file(private, "a-room.md", "y", None)

        context = await build_turn_context(k.service, agent.definition)
        text = context.text
        assert (
            "- household (id " + shared + ", read): Facts about the occupants." in text
        )
        assert f"({private}" not in text  # the id appears with a comma
        assert f"id {private}, read_write" in text
        # The index is sorted by name and cut at 50 entries.
        household = next(s for s in context.stores if s.name == "household")
        assert household.total_entries == 61
        assert household.entries[:2] == ("MAIN.md", "note-00.md")
        assert len(household.entries) == 50
        assert "- ... 11 more (61 entries in total; use knowledge.list)" in text
        # Only the private store injects its main file (the default).
        assert "Lounge warms in 40 minutes." in text
        assert "Shared main, not injected." not in text
        mine = next(s for s in context.stores if s.store_id == private)
        assert mine.entries == ("a-room.md", "MAIN.md")
        assert mine.main_file_size == len("Lounge warms in 40 minutes.")

        # Stable: the same input gives the same text; a change to a store
        # that the agent does not attach does not change it.
        other = await _store(k, "unrelated")
        await k.service.put_file(other, "z.md", "z", None)
        again = await build_turn_context(k.service, agent.definition)
        assert again.text == text
        smaller = await build_turn_context(k.service, agent.definition, index_entries=5)
        assert "- ... 56 more (61 entries in total" in smaller.text
        assert (await k.component.turn_context(agent.definition)).text == text


async def test_turn_context_index_size_option(tmp_path: Path) -> None:
    async with knowledge_env(tmp_path) as k:
        sid = await _store(k, "few")
        for name in ("c.md", "a.md", "b.md"):
            await k.service.put_file(sid, name, "x", None)
        agent = await k.agent("Reader", [{"store_id": sid}])
        context = await k.component.turn_context(agent.definition, 2)
        store = next(s for s in context.stores if s.store_id == sid)
        assert store.entries == ("a.md", "b.md")
        assert "- ... 1 more (3 entries in total" in context.text
