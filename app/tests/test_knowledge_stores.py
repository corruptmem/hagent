"""Knowledge stores, files, versions and documents (design section 13.4)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from helpers.knowledge import KnowledgeEnv, knowledge_env
from helpers.pdf import make_pdf


@pytest.fixture
async def k(tmp_path: Path) -> AsyncIterator[KnowledgeEnv]:
    async with knowledge_env(tmp_path) as env:
        yield env


async def _create_store(k: KnowledgeEnv, name: str, **body: Any) -> dict[str, Any]:
    status, data = await k.request(
        "POST", "/api/knowledge/stores", {"name": name, **body}
    )
    assert status == 201, data
    return data  # type: ignore[no-any-return]


# ------------------------------------------------------------------ AC 1


async def test_store_create_describe_limit_attach_and_share(k: KnowledgeEnv) -> None:
    store = await _create_store(
        k, "heating", description="Facts about the heating.", size_limit=2000
    )
    assert store["size_limit"] == 2000
    assert store["private"] is False
    assert store["attached"] == []

    status, data = await k.request("POST", "/api/knowledge/stores", {"name": "HEATING"})
    assert (status, data["error"]["code"]) == (409, "name_conflict")
    status, data = await k.request(
        "POST", "/api/knowledge/stores", {"name": "bad name!"}
    )
    assert (status, data["error"]["code"]) == (422, "invalid_name")

    # Attach the store to two agents, with different access.
    a = await k.agent("Heater", [{"store_id": store["id"], "access": "read_write"}])
    b = await k.agent("Water", [{"store_id": store["id"], "access": "read"}])
    status, data = await k.request("GET", f"/api/knowledge/stores/{store['id']}")
    assert status == 200
    attached = {(x["agent_id"], x["access"]) for x in data["attached"]}
    assert attached == {(a.id, "read_write"), (b.id, "read")}

    # Shared: what one agent writes, the other reads.
    wrote = await k.call(
        a,
        "knowledge.write",
        {
            "store_id": store["id"],
            "path": "boiler.md",
            "mode": "create",
            "content": "The boiler is in the loft.",
        },
    )
    assert wrote.ok, wrote
    read = await k.call(
        b, "knowledge.read", {"store_id": store["id"], "path": "boiler.md"}
    )
    assert read.ok and read.data["content"] == "The boiler is in the loft."
    assert read.data["version"] == 1

    # A private store can be shared too, and the list shows every store.
    assert a.private_store is not None
    await k.agent("Reader", [{"store_id": a.private_store.id, "access": "read"}])
    status, data = await k.request("GET", "/api/knowledge/stores")
    by_name = {s["name"]: s for s in data["stores"]}
    assert by_name["heating"]["files"] == 1
    private = by_name[a.private_store.name]
    assert private["private"] is True
    assert len(private["attached"]) == 2

    # Description and limit can change. A limit below the usage fails.
    status, data = await k.request(
        "PATCH",
        f"/api/knowledge/stores/{store['id']}",
        {"description": "Heating equipment only.", "size_limit": 5000},
    )
    assert status == 200
    assert (data["description"], data["size_limit"]) == (
        "Heating equipment only.",
        5000,
    )
    status, data = await k.request(
        "PATCH", f"/api/knowledge/stores/{store['id']}", {"size_limit": 1000}
    )
    assert status == 200  # 26 bytes used
    status, data = await k.request(
        "PATCH", f"/api/knowledge/stores/{store['id']}", {"size_limit": 10}
    )
    assert (status, data["error"]["code"]) == (422, "invalid_size_limit")

    # A store that live agents attach cannot be deleted.
    status, data = await k.request("DELETE", f"/api/knowledge/stores/{store['id']}")
    assert (status, data["error"]["code"]) == (409, "store_in_use")
    assert set(data["error"]["details"]["agent_ids"]) == {a.id, b.id}


async def test_size_limit_stops_writes_and_uploads(k: KnowledgeEnv) -> None:
    store = await _create_store(k, "small", size_limit=1000)
    agent = await k.agent("Writer", [{"store_id": store["id"], "access": "read_write"}])
    ok = await k.call(
        agent,
        "knowledge.write",
        {
            "store_id": store["id"],
            "path": "a.md",
            "mode": "create",
            "content": "x" * 900,
        },
    )
    assert ok.ok
    full = await k.call(
        agent,
        "knowledge.write",
        {
            "store_id": store["id"],
            "path": "a.md",
            "mode": "append",
            "content": "y" * 200,
        },
    )
    assert full.error_code == "size_limit"
    status, data = await k.request(
        "POST",
        f"/api/knowledge/stores/{store['id']}/documents",
        params={"name": "big.txt"},
        data=b"z" * 500,
        headers={"Content-Type": "text/plain"},
    )
    assert (status, data["error"]["code"]) == (413, "size_limit")


async def test_store_delete_removes_items_and_index(k: KnowledgeEnv) -> None:
    store = await _create_store(k, "temp")
    await k.service.put_file(store["id"], "notes.md", "# Notes\nboiler", None)
    assert await k.rows(
        "SELECT id FROM search_passages WHERE store_id = ?", store["id"]
    )
    status, _ = await k.request("DELETE", f"/api/knowledge/stores/{store['id']}")
    assert status == 200
    assert not await k.rows("SELECT * FROM knowledge_items")
    assert not await k.rows("SELECT * FROM search_passages WHERE store_id IS NOT NULL")
    hits = await k.rows("SELECT rowid FROM search_fts WHERE search_fts MATCH 'boiler'")
    assert hits == []


async def test_agent_delete_with_its_private_store_cascades(k: KnowledgeEnv) -> None:
    agent = await k.agent("Gone")
    assert agent.private_store is not None
    store_id = agent.private_store.id
    wrote = await k.call(
        agent,
        "knowledge.write",
        {
            "store_id": store_id,
            "path": "MAIN.md",
            "mode": "create",
            "content": "remember the radiator",
        },
    )
    assert wrote.ok
    await k.h.agents.delete(agent.id, "delete")
    assert not await k.rows("SELECT * FROM knowledge_items")
    assert not await k.rows("SELECT * FROM knowledge_item_versions")
    fts = await k.rows("SELECT rowid FROM search_fts WHERE search_fts MATCH 'radiator'")
    assert fts == []


# ------------------------------------------------------------------ AC 2


async def test_write_checks_versions_and_records_authors(k: KnowledgeEnv) -> None:
    store = await _create_store(k, "shared")
    entry = {"store_id": store["id"], "access": "read_write"}
    a = await k.agent("Alpha", [entry])
    b = await k.agent("Beta", [entry])
    sid = store["id"]

    first = await k.call(
        a,
        "knowledge.write",
        {
            "store_id": sid,
            "path": "rooms.md",
            "mode": "create",
            "content": "# Rooms\nLounge warms in 40 minutes.",
        },
    )
    assert first.ok and first.data["version"] == 1
    again = await k.call(
        b,
        "knowledge.write",
        {"store_id": sid, "path": "rooms.md", "mode": "create", "content": "x"},
    )
    assert again.error_code == "file_exists"

    # Both agents read version 1. Beta writes first; Alpha's write is stale.
    second = await k.call(
        b,
        "knowledge.write",
        {
            "store_id": sid,
            "path": "rooms.md",
            "mode": "replace",
            "old_text": "40 minutes",
            "content": "35 minutes",
            "base_version": 1,
        },
    )
    assert second.ok and second.data["version"] == 2
    stale = await k.call(
        a,
        "knowledge.write",
        {
            "store_id": sid,
            "path": "rooms.md",
            "mode": "replace",
            "old_text": "Lounge",
            "content": "Living room",
            "base_version": 1,
        },
    )
    assert stale.error_code == "version_conflict"
    assert "version 2" in (stale.error or "")
    no_base = await k.call(
        a,
        "knowledge.write",
        {
            "store_id": sid,
            "path": "rooms.md",
            "mode": "replace",
            "old_text": "Lounge",
            "content": "Living room",
        },
    )
    assert no_base.error_code == "version_required"
    missing = await k.call(
        a,
        "knowledge.write",
        {
            "store_id": sid,
            "path": "rooms.md",
            "mode": "replace",
            "old_text": "Kitchen",
            "content": "x",
            "base_version": 2,
        },
    )
    assert missing.error_code == "old_text_not_found"
    appended = await k.call(
        a,
        "knowledge.write",
        {
            "store_id": sid,
            "path": "rooms.md",
            "mode": "append",
            "content": "Bedroom warms in 20 minutes.",
            "base_version": 2,
        },
    )
    assert appended.ok and appended.data["version"] == 3

    # The user edits the file through the management API.
    status, data = await k.request(
        "PUT",
        f"/api/knowledge/stores/{sid}/files/rooms.md",
        {"content": "# Rooms\nUser text.", "base_version": 3},
    )
    assert status == 200 and data["version"] == 4
    status, data = await k.request(
        "PUT",
        f"/api/knowledge/stores/{sid}/files/rooms.md",
        {"content": "late", "base_version": 3},
    )
    assert (status, data["error"]["code"]) == (409, "version_conflict")

    # The agent deletes the file; the delete is a version too.
    deleted = await k.call(
        b, "knowledge.delete", {"store_id": sid, "path": "rooms.md", "base_version": 4}
    )
    assert deleted.ok

    status, data = await k.request(
        "GET", f"/api/knowledge/stores/{sid}/history/rooms.md"
    )
    assert status == 200
    history = [
        (v["version"], v["change"], v["author"], v["author_agent_id"], v["deleted"])
        for v in data["versions"]
    ]
    assert history == [
        (5, "delete", "agent", b.id, True),
        (4, "put", "user", None, False),
        (3, "append", "agent", a.id, False),
        (2, "replace", "agent", b.id, False),
        (1, "create", "agent", a.id, False),
    ]

    # Restore version 3 as a new version 6.
    status, data = await k.request(
        "GET", f"/api/knowledge/stores/{sid}/history/rooms.md", params={"version": "3"}
    )
    assert "35 minutes" in data["content"] and "Bedroom" in data["content"]
    status, data = await k.request(
        "POST",
        f"/api/knowledge/stores/{sid}/restore",
        {"path": "rooms.md", "version": 3},
    )
    assert status == 200 and data["version"] == 6
    read = await k.call(a, "knowledge.read", {"store_id": sid, "path": "rooms.md"})
    assert read.ok and read.data["version"] == 6
    assert "Bedroom warms in 20 minutes." in read.data["content"]
    status, data = await k.request(
        "POST",
        f"/api/knowledge/stores/{sid}/restore",
        {"path": "rooms.md", "version": 5},
    )
    assert (status, data["error"]["code"]) == (409, "not_restorable")
    # A stale write after the restore fails too.
    stale = await k.call(
        a,
        "knowledge.write",
        {
            "store_id": sid,
            "path": "rooms.md",
            "mode": "append",
            "content": "x",
            "base_version": 3,
        },
    )
    assert stale.error_code == "version_conflict"


async def test_read_section_of_a_file(k: KnowledgeEnv) -> None:
    store = await _create_store(k, "notes")
    agent = await k.agent("Reader", [{"store_id": store["id"]}])
    await k.service.put_file(
        store["id"], "MAIN.md", "# Boiler\nIn the loft.\n# Pump\nIn the cupboard.", None
    )
    read = await k.call(
        agent,
        "knowledge.read",
        {"store_id": store["id"], "path": "MAIN.md", "section": "pump"},
    )
    assert read.ok and read.data["content"] == "In the cupboard."
    listing = await k.call(agent, "knowledge.list", {"store_id": store["id"]})
    assert listing.ok
    assert [(i["path"], i["version"]) for i in listing.data] == [("MAIN.md", 1)]


# ------------------------------------------------------------------ AC 3


async def test_documents_are_extracted_split_indexed_and_named(
    k: KnowledgeEnv,
) -> None:
    store = await _create_store(k, "manuals")
    sid = store["id"]
    agent = await k.agent("Reader", [{"store_id": sid}])
    uploads = [
        (
            "Boiler Manual (2019).pdf",
            "application/pdf",
            make_pdf(["Contents", "Pressure\nTop up the boiler pressure to 1.5 bar."]),
        ),
        (
            "radiators.md",
            "text/markdown",
            b"# Bleeding\nBleed each radiator once a year.\n# Valves\nTurn slowly.",
        ),
        ("pump.txt", "text/plain", b"The pump runs at speed two."),
        (
            "thermostat.html",
            "text/html",
            b"<h1>Schedule</h1><p>The thermostat schedule starts at 06:30.</p>",
        ),
    ]
    for name, media, data in uploads:
        status, body = await k.request(
            "POST",
            f"/api/knowledge/stores/{sid}/documents",
            params={"name": name},
            data=data,
            headers={"Content-Type": media},
        )
        assert status == 201, body
        assert body["passages"] >= 1

    status, body = await k.request("GET", f"/api/knowledge/stores/{sid}/items")
    paths = [i["path"] for i in body["items"]]
    assert paths == [
        "Boiler Manual _2019_.pdf",
        "pump.txt",
        "radiators.md",
        "thermostat.html",
    ]
    pdf = body["items"][0]
    assert (pdf["kind"], pdf["media_type"], pdf["pages"]) == (
        "document",
        "application/pdf",
        2,
    )

    result = await k.call(
        agent, "knowledge.search", {"query": "boiler pressure", "limit": 3}
    )
    assert result.ok, result
    top = result.data[0]
    assert top["path"] == "Boiler Manual _2019_.pdf"
    assert top["page"] == 2
    assert top["store"] == "manuals"
    result = await k.call(agent, "knowledge.search", {"query": "bleed radiator"})
    assert result.data[0]["path"] == "radiators.md"
    assert result.data[0]["heading"] == "Bleeding"
    result = await k.call(agent, "knowledge.search", {"query": "thermostat schedule"})
    assert result.data[0]["path"] == "thermostat.html"
    assert result.data[0]["heading"] == "Schedule"

    # Read by page, and agents cannot change or delete documents.
    page = await k.call(
        agent,
        "knowledge.read",
        {"store_id": sid, "path": "Boiler Manual _2019_.pdf", "section": "page 2"},
    )
    assert page.ok and "1.5 bar" in page.data["content"]
    writer = await k.agent("Writer", [{"store_id": sid, "access": "read_write"}])
    denied = await k.call(
        writer, "knowledge.delete", {"store_id": sid, "path": "pump.txt"}
    )
    assert denied.error_code == "document_read_only"
    denied = await k.call(
        writer,
        "knowledge.write",
        {"store_id": sid, "path": "pump.txt", "mode": "append", "content": "x"},
    )
    assert denied.error_code == "document_read_only"

    # Replace a document: the index follows.
    status, body = await k.request(
        "PUT",
        f"/api/knowledge/stores/{sid}/documents/pump.txt",
        data=b"The circulation pump now runs at speed three.",
        headers={"Content-Type": "text/plain"},
    )
    assert status == 200 and body["version"] == 2
    result = await k.call(agent, "knowledge.search", {"query": "speed two"})
    texts = [r["text"] for r in result.data if r["path"] == "pump.txt"]
    assert texts == ["The circulation pump now runs at speed three."]

    # The user deletes a document: the index follows.
    status, body = await k.request(
        "DELETE", f"/api/knowledge/stores/{sid}/items/thermostat.html"
    )
    assert status == 200
    result = await k.call(agent, "knowledge.search", {"query": "thermostat schedule"})
    assert all(r["path"] != "thermostat.html" for r in result.data)


async def test_upload_errors(k: KnowledgeEnv) -> None:
    store = await _create_store(k, "docs", size_limit=10_000_000)
    base = f"/api/knowledge/stores/{store['id']}/documents"
    status, body = await k.request(
        "POST",
        base,
        params={"name": "scan.pdf"},
        data=make_pdf([None]),
        headers={"Content-Type": "application/pdf"},
    )
    assert (status, body["error"]["code"]) == (422, "extract_failed")
    status, body = await k.request(
        "POST",
        base,
        params={"name": "a.docx"},
        data=b"x",
        headers={"Content-Type": "application/octet-stream"},
    )
    assert (status, body["error"]["code"]) == (415, "unsupported_document")
    # Bodies larger than aiohttp's 1 MiB default are accepted.
    big = ("paragraph about the boiler. " * 40 + "\n\n") * 1200
    assert len(big) > 1_100_000
    status, body = await k.request(
        "POST",
        base,
        params={"name": "big.txt"},
        data=big.encode(),
        headers={"Content-Type": "text/plain"},
    )
    assert status == 201, body
    status, body = await k.request(
        "POST",
        base,
        params={"name": "big.txt"},
        data=b"again",
        headers={"Content-Type": "text/plain"},
    )
    assert (status, body["error"]["code"]) == (409, "document_exists")
    # A large document is read in parts.
    agent = await k.agent("Reader", [{"store_id": store["id"]}])
    first = await k.call(
        agent, "knowledge.read", {"store_id": store["id"], "path": "big.txt"}
    )
    assert first.ok
    assert first.data["part"] == 1 and first.data["parts"] > 10
    assert first.data["next_section"] == "part 2"


async def test_upload_over_the_option_limit(tmp_path: Path) -> None:
    async with knowledge_env(tmp_path, options={"max_upload_bytes": 1000}) as k:
        store = await _create_store(k, "docs")
        status, body = await k.request(
            "POST",
            f"/api/knowledge/stores/{store['id']}/documents",
            params={"name": "a.txt"},
            data=b"x" * 2000,
            headers={"Content-Type": "text/plain"},
        )
        assert (status, body["error"]["code"]) == (413, "upload_too_large")


async def test_change_events(k: KnowledgeEnv) -> None:
    store = await _create_store(k, "events")
    await k.service.put_file(store["id"], "a.md", "text", None)
    actions = [(e.topic, e.action) for e in k.events]
    assert ("knowledge", "created") in actions
    assert ("knowledge", "file_changed") in actions
