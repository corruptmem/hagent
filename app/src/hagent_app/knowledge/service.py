"""Knowledge stores, files, documents and change history (design section 13.4).

``KnowledgeService`` serves the management API and the knowledge tools. It
implements the ``KnowledgeStore`` protocol of the tool executor. The
executor checks the attachments before it calls the service, so the service
trusts the store IDs that it receives.

Terms:

- **File**: Markdown text that agents and the user write. ``MAIN.md`` is the
  main file of a store.
- **Document**: an upload. Agents can read and search it, but not change it.
- **Item**: a file or a document. Files and documents share one path
  namespace in a store.
- **Version**: a number that grows by one on each change of an item. A
  write names the version that the writer read (``base_version``). If the
  item has changed since, the write fails with ``version_conflict``.
- **Author**: the agent that made a change, or the user (``NULL``).
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from hagent_app.agents import AgentReader
from hagent_app.http.events import ChangeEvents
from hagent_app.knowledge.index import (
    IndexEntry,
    Scope,
    SearchIndex,
    SearchResult,
    delete_source,
    hit_json,
    replace_source,
)
from hagent_app.knowledge.text import (
    Extracted,
    ExtractError,
    MediaType,
    Section,
    extract,
    markdown_sections,
    split,
)
from hagent_app.storage import Database, transaction
from hagent_app.tools.stores import StoreError, WriteMode

MAIN_FILE = "MAIN.md"
READ_PART_CHARS = 20_000
MIN_SIZE_LIMIT = 1_000
MAX_SIZE_LIMIT = 1_000_000_000
STORE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")
_PAGE = re.compile(r"^page\s+(\d{1,6})$", re.IGNORECASE)
_PART = re.compile(r"^part\s+(\d{1,6})$", re.IGNORECASE)


class KnowledgeError(StoreError):
    """A refused knowledge call. ``status`` is the HTTP status for the API."""

    def __init__(
        self, status: int, code: str, message: str, details: Any = None
    ) -> None:
        super().__init__(code, message)
        self.status = status
        self.details = details


def _error(status: int, code: str, message: str, details: Any = None) -> KnowledgeError:
    return KnowledgeError(status, code, message, details)


def _not_found(what: str) -> KnowledgeError:
    return KnowledgeError(404, "not_found", f"{what} does not exist")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _size(text: str) -> int:
    return len(text.encode("utf-8"))


@dataclass(frozen=True, slots=True)
class StoreInfo:
    """One store row with its usage."""

    id: str
    name: str
    description: str
    private: bool
    owner_agent_id: str | None
    size_limit: int
    size: int
    files: int
    documents: int
    created_at: str
    updated_at: str | None

    def to_data(self) -> dict[str, Any]:
        """The JSON form."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "private": self.private,
            "owner_agent_id": self.owner_agent_id,
            "size_limit": self.size_limit,
            "size": self.size,
            "files": self.files,
            "documents": self.documents,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


_STORE_SQL = (
    "SELECT s.id, s.name, s.description, s.private, s.owner_agent_id, "
    "s.size_limit, s.created_at, s.updated_at, "
    "COALESCE(SUM(i.size), 0) AS size, "
    "COUNT(CASE WHEN i.kind = 'file' THEN 1 END) AS files, "
    "COUNT(CASE WHEN i.kind = 'document' THEN 1 END) AS documents "
    "FROM knowledge_stores s LEFT JOIN knowledge_items i ON i.store_id = s.id "
)


def _store(row: sqlite3.Row) -> StoreInfo:
    return StoreInfo(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        private=bool(row["private"]),
        owner_agent_id=row["owner_agent_id"],
        size_limit=int(row["size_limit"]),
        size=int(row["size"]),
        files=int(row["files"]),
        documents=int(row["documents"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def get_store(conn: sqlite3.Connection, store_id: str) -> StoreInfo:
    """One store. Raise ``not_found``."""
    row = conn.execute(
        _STORE_SQL + "WHERE s.id = ? GROUP BY s.id", (store_id,)
    ).fetchone()
    if row is None:
        raise _not_found("the store")  # noqa: TRY003
    return _store(row)


def list_stores(
    conn: sqlite3.Connection, store_ids: Sequence[str] | None = None
) -> list[StoreInfo]:
    """Stores sorted by name: all, or the named ones."""
    if store_ids is None:
        rows = conn.execute(
            _STORE_SQL + "GROUP BY s.id ORDER BY s.name COLLATE NOCASE"
        ).fetchall()
    else:
        rows = conn.execute(
            _STORE_SQL  # noqa: S608 - fixed SQL
            + "WHERE s.id IN (SELECT value FROM json_each(?)) "
            "GROUP BY s.id ORDER BY s.name COLLATE NOCASE",
            (json.dumps(list(store_ids)),),
        ).fetchall()
    return [_store(r) for r in rows]


def _item(conn: sqlite3.Connection, store_id: str, path: str) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM knowledge_items WHERE store_id = ? AND path = ? COLLATE NOCASE",
        (store_id, path),
    ).fetchone()
    return row


def _next_version(conn: sqlite3.Connection, store_id: str, path: str) -> int:
    row = conn.execute(
        "SELECT MAX(version) FROM knowledge_item_versions "
        "WHERE store_id = ? AND path = ?",
        (store_id, path),
    ).fetchone()
    return int(row[0] or 0) + 1


def _check_size(
    conn: sqlite3.Connection, store_id: str, old_size: int, new_size: int
) -> None:
    store = get_store(conn, store_id)
    total = store.size - old_size + new_size
    if new_size > old_size and total > store.size_limit:
        raise _error(
            413,
            "size_limit",
            f"the store would hold {total} bytes; its limit is {store.size_limit}",
            {"size": total, "size_limit": store.size_limit},
        )


def _check_base(row: sqlite3.Row, base_version: int | None, *, required: bool) -> None:
    if base_version is None:
        if required:
            raise _error(
                409,
                "version_required",
                f"give base_version; the file is at version {row['version']}",
            )
        return
    if int(row["version"]) != base_version:
        raise _error(
            409,
            "version_conflict",
            f"the file is at version {row['version']}, not {base_version}; "
            "read it again",
            {"version": int(row["version"])},
        )


def _index_file(
    conn: sqlite3.Connection,
    store_id: str,
    path: str,
    content: str,
    author: str | None,
) -> None:
    entry = IndexEntry(
        "knowledge", path, title=path, store_id=store_id, charge_agent_id=author
    )
    replace_source(conn, entry, split(markdown_sections(content)))


def _item_json(row: sqlite3.Row) -> dict[str, Any]:
    data = {
        "path": row["path"],
        "kind": row["kind"],
        "version": int(row["version"]),
        "size": int(row["size"]),
        "updated_at": row["updated_at"],
        "updated_by_agent_id": row["updated_by_agent_id"],
    }
    if row["kind"] == "document":
        data["media_type"] = row["media_type"]
        data["pages"] = row["pages"]
    return data


def put_text(  # noqa: PLR0912, PLR0913
    conn: sqlite3.Connection,
    store_id: str,
    path: str,
    mode: WriteMode | str,
    content: str,
    *,
    old_text: str | None = None,
    base_version: int | None = None,
    author: str | None = None,
    change: str | None = None,
) -> dict[str, Any]:
    """Write a file. ``mode`` is create, append, replace or put (whole text).

    Rules: create fails if the path exists. Append, replace and put need an
    existing file. Replace and put need ``base_version``. Append checks it
    if given. ``author`` ``None`` means the user.
    """
    with transaction(conn):
        get_store(conn, store_id)
        row = _item(conn, store_id, path)
        if row is not None and row["kind"] == "document":
            raise _error(
                409, "document_read_only", "documents cannot be changed this way"
            )
        if mode == "create":
            if row is not None:
                raise _error(
                    409,
                    "file_exists",
                    f"the file exists at version {row['version']}; "
                    "use append or replace",
                    {"version": int(row["version"])},
                )
            text = content
        elif row is None:
            if mode == "put" and base_version is None:
                text = content
            else:
                raise _not_found("the file")  # noqa: TRY003
        else:
            _check_base(row, base_version, required=mode in {"replace", "put"})
            old = str(row["content"])
            if mode == "append":
                sep = "" if not old or old.endswith("\n") else "\n"
                text = old + sep + content
            elif mode == "replace":
                count = old.count(old_text or "") if old_text else 0
                if count == 0:
                    raise _error(
                        409, "old_text_not_found", "old_text is not in the file"
                    )
                if count > 1:
                    raise _error(
                        409,
                        "old_text_not_unique",
                        f"old_text occurs {count} times; give more context",
                    )
                text = old.replace(old_text or "", content, 1)
            else:
                text = content
        old_size = int(row["size"]) if row is not None else 0
        _check_size(conn, store_id, old_size, _size(text))
        version = _next_version(conn, store_id, path)
        now = _now()
        if row is None:
            conn.execute(
                "INSERT INTO knowledge_items (store_id, path, kind, version, "
                "content, size, created_at, updated_at, updated_by_agent_id) "
                "VALUES (?, ?, 'file', ?, ?, ?, ?, ?, ?)",
                (store_id, path, version, text, _size(text), now, now, author),
            )
        else:
            path = row["path"]
            conn.execute(
                "UPDATE knowledge_items SET version = ?, content = ?, size = ?, "
                "updated_at = ?, updated_by_agent_id = ? WHERE id = ?",
                (version, text, _size(text), now, author, row["id"]),
            )
        conn.execute(
            "INSERT INTO knowledge_item_versions (store_id, path, version, kind, "
            "change, content, author_agent_id, created_at) "
            "VALUES (?, ?, ?, 'file', ?, ?, ?, ?)",
            (store_id, path, version, change or mode, text, author, now),
        )
        conn.execute(
            "UPDATE knowledge_stores SET updated_at = ? WHERE id = ?", (now, store_id)
        )
        _index_file(conn, store_id, path, text, author)
    return {"store_id": store_id, "path": path, "version": version, "size": _size(text)}


def delete_item(  # noqa: PLR0913
    conn: sqlite3.Connection,
    store_id: str,
    path: str,
    *,
    base_version: int | None,
    author: str | None,
    allow_document: bool,
) -> dict[str, Any]:
    """Delete a file or a document. Record a tombstone version."""
    with transaction(conn):
        row = _item(conn, store_id, path)
        if row is None:
            raise _not_found("the item")  # noqa: TRY003
        if row["kind"] == "document" and not allow_document:
            raise _error(409, "document_read_only", "agents cannot delete documents")
        _check_base(row, base_version, required=False)
        version = _next_version(conn, store_id, row["path"])
        now = _now()
        conn.execute("DELETE FROM knowledge_items WHERE id = ?", (row["id"],))
        conn.execute(
            "INSERT INTO knowledge_item_versions (store_id, path, version, kind, "
            "change, content, deleted, author_agent_id, created_at) "
            "VALUES (?, ?, ?, ?, 'delete', NULL, 1, ?, ?)",
            (store_id, row["path"], version, row["kind"], author, now),
        )
        conn.execute(
            "UPDATE knowledge_stores SET updated_at = ? WHERE id = ?", (now, store_id)
        )
        delete_source(
            conn,
            IndexEntry("knowledge", row["path"], store_id=store_id),
        )
    return {"store_id": store_id, "path": row["path"], "version": version}


def _save_document(
    conn: sqlite3.Connection,
    store_id: str,
    path: str,
    extracted: Extracted,
    *,
    replace: bool,
) -> dict[str, Any]:
    text = extracted.text
    with transaction(conn):
        get_store(conn, store_id)
        row = _item(conn, store_id, path)
        if row is not None and row["kind"] == "file":
            raise _error(409, "path_in_use", "a file uses this path")
        if row is not None and not replace:
            raise _error(
                409, "document_exists", "the document exists; replace it with PUT"
            )
        if row is None and replace:
            raise _not_found("the document")  # noqa: TRY003
        old_size = int(row["size"]) if row is not None else 0
        _check_size(conn, store_id, old_size, _size(text))
        version = _next_version(conn, store_id, path)
        now = _now()
        if row is not None:
            path = row["path"]
            conn.execute("DELETE FROM knowledge_items WHERE id = ?", (row["id"],))
        cursor = conn.execute(
            "INSERT INTO knowledge_items (store_id, path, kind, version, content, "
            "size, media_type, pages, created_at, updated_at) "
            "VALUES (?, ?, 'document', ?, ?, ?, ?, ?, ?, ?)",
            (
                store_id,
                path,
                version,
                text,
                _size(text),
                extracted.media_type,
                extracted.pages,
                row["created_at"] if row is not None else now,
                now,
            ),
        )
        conn.executemany(
            "INSERT INTO knowledge_document_sections (item_id, ordinal, heading, "
            "page, text) VALUES (?, ?, ?, ?, ?)",
            [
                (cursor.lastrowid, n, s.heading, s.page, s.text)
                for n, s in enumerate(extracted.sections)
            ],
        )
        conn.execute(
            "INSERT INTO knowledge_item_versions (store_id, path, version, kind, "
            "change, content, author_agent_id, created_at) "
            "VALUES (?, ?, ?, 'document', ?, NULL, NULL, ?)",
            (store_id, path, version, "replace" if replace else "upload", now),
        )
        conn.execute(
            "UPDATE knowledge_stores SET updated_at = ? WHERE id = ?", (now, store_id)
        )
        entry = IndexEntry("knowledge", path, title=path, store_id=store_id)
        passages = replace_source(conn, entry, split(extracted.sections))
    return {
        "store_id": store_id,
        "path": path,
        "version": version,
        "size": _size(text),
        "media_type": extracted.media_type,
        "pages": extracted.pages,
        "sections": len(extracted.sections),
        "passages": passages,
    }


def _read(
    conn: sqlite3.Connection, store_id: str, path: str, section: str | None
) -> dict[str, Any]:
    row = _item(conn, store_id, path)
    if row is None:
        raise _not_found("the item")  # noqa: TRY003
    data = _item_json(row)
    data["store_id"] = store_id
    if row["kind"] == "file":
        sections = markdown_sections(str(row["content"]))
        outline: list[dict[str, Any]] = [
            {"heading": s.heading} for s in sections if s.heading
        ]
    else:
        rows = conn.execute(
            "SELECT heading, page, text FROM knowledge_document_sections "
            "WHERE item_id = ? ORDER BY ordinal",
            (row["id"],),
        ).fetchall()
        sections = [_section(r) for r in rows]
        outline = [
            {k: v for k, v in (("heading", s.heading), ("page", s.page)) if v}
            for s in sections
        ]
    content = str(row["content"])
    parts = max(1, -(-len(content) // READ_PART_CHARS))
    if section is None:
        if parts == 1:
            data["content"] = content
            return data
        section = "part 1"
    if match := _PART.match(section.strip()):
        number = int(match.group(1))
        if not 1 <= number <= parts:
            raise _not_found(f"part {number}")  # noqa: TRY003
        start = (number - 1) * READ_PART_CHARS
        data["content"] = content[start : start + READ_PART_CHARS]
        data["part"] = number
        data["parts"] = parts
        data["outline"] = outline[:200]
        if number < parts:
            data["next_section"] = f"part {number + 1}"
        return data
    if match := _PAGE.match(section.strip()):
        page = int(match.group(1))
        texts = [s.text for s in sections if s.page == page]
        if not texts:
            raise _not_found(f"page {page}")  # noqa: TRY003
        data["page"] = page
        data["content"] = "\n\n".join(texts)
        return data
    wanted = section.strip().lower()
    texts = [s.text for s in sections if (s.heading or "").lower() == wanted]
    if not texts:
        raise _not_found(f"the section {section!r}")  # noqa: TRY003
    data["section"] = section
    data["content"] = "\n\n".join(texts)
    return data


def _section(row: sqlite3.Row) -> Section:
    return Section(row["heading"], row["page"], row["text"])


class KnowledgeService:
    """Stores, files and documents. Implements ``KnowledgeStore``."""

    def __init__(
        self,
        db: Database,
        index: SearchIndex,
        *,
        events: ChangeEvents | None = None,
        on_change: Callable[[], None] | None = None,
        default_size_limit: int = 1_000_000,
    ) -> None:
        self._db = db
        self.index = index
        self._events = events
        self._on_change = on_change
        self._default_size_limit = default_size_limit
        self._agents = AgentReader(db)

    def _changed(self, action: str, store_id: str, data: dict[str, Any]) -> None:
        if self._on_change is not None:
            self._on_change()
        if self._events is not None:
            self._events.publish("knowledge", action, store_id, data)

    # ------------------------------------------------------------ stores

    async def stores(self) -> list[dict[str, Any]]:
        """Every store, with the agents that attach it."""
        stores = await self._db.run(list_stores)
        attached = await self._attachments()
        return [{**s.to_data(), "attached": attached.get(s.id, [])} for s in stores]

    async def store(self, store_id: str) -> dict[str, Any]:
        """One store, with the agents that attach it."""
        store = await self._db.run(lambda c: get_store(c, store_id))
        attached = await self._attachments()
        return {**store.to_data(), "attached": attached.get(store_id, [])}

    async def _attachments(self) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = {}
        for agent in await self._agents.list_agents():
            for a in agent.definition.knowledge:
                result.setdefault(a.store_id, []).append(
                    {
                        "agent_id": agent.id,
                        "agent_name": agent.name,
                        "access": a.access,
                        "inject_main": a.inject_main,
                    }
                )
        return result

    async def create_store(
        self, name: str, description: str, size_limit: int | None
    ) -> dict[str, Any]:
        """Create a shared store."""
        _check_name(name)
        limit = self._default_size_limit if size_limit is None else size_limit
        _check_limit(limit)
        store_id = uuid.uuid4().hex

        def create(conn: sqlite3.Connection) -> None:
            with transaction(conn):
                _check_free_name(conn, name, None)
                conn.execute(
                    "INSERT INTO knowledge_stores (id, name, description, private, "
                    "size_limit, created_at, updated_at) VALUES (?, ?, ?, 0, ?, ?, ?)",
                    (store_id, name, description, limit, _now(), _now()),
                )

        await self._db.run(create)
        self._changed("created", store_id, {"name": name})
        return await self.store(store_id)

    async def update_store(
        self,
        store_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        size_limit: int | None = None,
    ) -> dict[str, Any]:
        """Change the name, description or size limit of a store."""
        if name is not None:
            _check_name(name)
        if size_limit is not None:
            _check_limit(size_limit)

        def update(conn: sqlite3.Connection) -> None:
            with transaction(conn):
                store = get_store(conn, store_id)
                if name is not None:
                    _check_free_name(conn, name, store_id)
                if size_limit is not None and size_limit < store.size:
                    raise _error(
                        409,
                        "size_limit",
                        f"the store holds {store.size} bytes, more than the limit",
                        {"size": store.size},
                    )
                conn.execute(
                    "UPDATE knowledge_stores SET name = COALESCE(?, name), "
                    "description = COALESCE(?, description), "
                    "size_limit = COALESCE(?, size_limit), updated_at = ? WHERE id = ?",
                    (name, description, size_limit, _now(), store_id),
                )

        await self._db.run(update)
        self._changed("updated", store_id, {})
        return await self.store(store_id)

    async def delete_store(self, store_id: str) -> None:
        """Delete a store that no live agent attaches."""
        store = await self._db.run(lambda c: get_store(c, store_id))
        attached = (await self._attachments()).get(store_id, [])
        if store.private or attached:
            raise _error(
                409,
                "store_in_use",
                "live agents attach the store; detach it first",
                {"agent_ids": [a["agent_id"] for a in attached]},
            )

        def delete(conn: sqlite3.Connection) -> None:
            with transaction(conn):
                conn.execute("DELETE FROM knowledge_stores WHERE id = ?", (store_id,))

        await self._db.run(delete)
        self._changed("deleted", store_id, {})

    # ---------------------------------------------- KnowledgeStore protocol

    async def describe(self, store_ids: Sequence[str]) -> list[dict[str, Any]]:
        """Name, description and size of each store."""
        stores = await self._db.run(lambda c: list_stores(c, store_ids))
        return [
            {
                "id": s.id,
                "name": s.name,
                "description": s.description,
                "size": s.size,
                "size_limit": s.size_limit,
                "files": s.files,
                "documents": s.documents,
            }
            for s in stores
        ]

    async def list_items(self, store_id: str) -> list[dict[str, Any]]:
        """The files and documents in one store, sorted by path."""

        def items(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            get_store(conn, store_id)
            rows = conn.execute(
                "SELECT * FROM knowledge_items WHERE store_id = ? "
                "ORDER BY path COLLATE NOCASE",
                (store_id,),
            ).fetchall()
            return [_item_json(r) for r in rows]

        return await self._db.run(items)

    async def read(
        self, store_id: str, path: str, section: str | None
    ) -> dict[str, Any]:
        """One item, or one section, page or part of it, with its version."""
        return await self._db.run(lambda c: _read(c, store_id, path, section))

    async def search(
        self, store_ids: Sequence[str], query: str, limit: int, *, agent_id: str
    ) -> list[dict[str, Any]]:
        """Hybrid search across the named stores only, for an agent."""
        result = await self.search_stores(store_ids, query, limit, agent_id=agent_id)
        names = {s["id"]: s["name"] for s in await self.describe(store_ids)}
        hits = []
        for hit in result.hits:
            data = hit_json(hit)
            data["store"] = names.get(hit.store_id or "")
            data["path"] = data.pop("source_id")
            data.pop("title", None)
            data.pop("collection", None)
            if result.mode != "hybrid":
                data["search_mode"] = "keyword"
            hits.append(data)
        return hits

    async def search_stores(
        self,
        store_ids: Sequence[str],
        query: str,
        limit: int,
        *,
        agent_id: str | None = None,
    ) -> SearchResult:
        """Hybrid search across the named stores, with the search mode.

        The query embedding counts towards ``agent_id`` (``None``: the
        installation, for a search from the management API).
        """
        scope = Scope(("knowledge",), store_ids=tuple(store_ids))
        return await self.index.search(scope, query, limit, charge_agent_id=agent_id)

    async def write(  # noqa: PLR0913
        self,
        store_id: str,
        path: str,
        mode: WriteMode,
        content: str,
        *,
        old_text: str | None,
        base_version: int | None,
        actor_agent_id: str,
    ) -> dict[str, Any]:
        """Create, append to or change a file. Fail on a version conflict."""
        result = await self._db.run(
            lambda c: put_text(
                c,
                store_id,
                path,
                mode,
                content,
                old_text=old_text,
                base_version=base_version,
                author=actor_agent_id,
            )
        )
        self._changed("file_changed", store_id, {"path": result["path"]})
        return result

    async def delete_file(
        self, store_id: str, path: str, *, base_version: int | None, actor_agent_id: str
    ) -> None:
        """Delete a file. Documents cannot be deleted this way."""
        result = await self._db.run(
            lambda c: delete_item(
                c,
                store_id,
                path,
                base_version=base_version,
                author=actor_agent_id,
                allow_document=False,
            )
        )
        self._changed("file_deleted", store_id, {"path": result["path"]})

    # ------------------------------------------------- user (management API)

    async def put_file(
        self, store_id: str, path: str, content: str, base_version: int | None
    ) -> dict[str, Any]:
        """The user writes the whole text of a file."""
        result = await self._db.run(
            lambda c: put_text(
                c, store_id, path, "put", content, base_version=base_version
            )
        )
        self._changed("file_changed", store_id, {"path": result["path"]})
        return result

    async def delete_user_item(
        self, store_id: str, path: str, base_version: int | None
    ) -> dict[str, Any]:
        """The user deletes a file or a document."""
        result = await self._db.run(
            lambda c: delete_item(
                c,
                store_id,
                path,
                base_version=base_version,
                author=None,
                allow_document=True,
            )
        )
        self._changed("item_deleted", store_id, {"path": result["path"]})
        return result

    async def history(self, store_id: str, path: str) -> list[dict[str, Any]]:
        """The change history of one item, newest first."""

        def rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            get_store(conn, store_id)
            found = conn.execute(
                "SELECT version, kind, change, deleted, author_agent_id, created_at, "
                "LENGTH(CAST(content AS BLOB)) AS size FROM knowledge_item_versions "
                "WHERE store_id = ? AND path = ? ORDER BY version DESC",
                (store_id, path),
            ).fetchall()
            if not found:
                raise _not_found("the item")  # noqa: TRY003
            return [
                {
                    "version": r["version"],
                    "kind": r["kind"],
                    "change": r["change"],
                    "deleted": bool(r["deleted"]),
                    "author": "agent" if r["author_agent_id"] else "user",
                    "author_agent_id": r["author_agent_id"],
                    "size": r["size"],
                    "created_at": r["created_at"],
                }
                for r in found
            ]

        return await self._db.run(rows)

    async def version(self, store_id: str, path: str, version: int) -> dict[str, Any]:
        """One recorded version of a file, with its text."""

        def row(conn: sqlite3.Connection) -> dict[str, Any]:
            found = conn.execute(
                "SELECT * FROM knowledge_item_versions "
                "WHERE store_id = ? AND path = ? AND version = ?",
                (store_id, path, version),
            ).fetchone()
            if found is None:
                raise _not_found("the version")  # noqa: TRY003
            return {
                "path": found["path"],
                "version": found["version"],
                "kind": found["kind"],
                "change": found["change"],
                "deleted": bool(found["deleted"]),
                "author_agent_id": found["author_agent_id"],
                "created_at": found["created_at"],
                "content": found["content"],
            }

        return await self._db.run(row)

    async def restore(
        self, store_id: str, path: str, version: int, base_version: int | None
    ) -> dict[str, Any]:
        """The user restores an earlier version of a file as a new version."""

        def restore(conn: sqlite3.Connection) -> dict[str, Any]:
            found = conn.execute(
                "SELECT path, content, kind, deleted FROM knowledge_item_versions "
                "WHERE store_id = ? AND path = ? AND version = ?",
                (store_id, path, version),
            ).fetchone()
            if found is None:
                raise _not_found("the version")  # noqa: TRY003
            if found["kind"] != "file" or found["deleted"] or found["content"] is None:
                raise _error(
                    409,
                    "not_restorable",
                    "only a version of a file with text can be restored",
                )
            current = _item(conn, store_id, path)
            if current is not None:
                _check_base(current, base_version, required=False)
                base = int(current["version"])
            else:
                base = None
            return put_text(
                conn,
                store_id,
                found["path"],
                "put",
                str(found["content"]),
                base_version=base,
                change=f"restore {version}",
            )

        result = await self._db.run(restore)
        self._changed("file_changed", store_id, {"path": result["path"]})
        return result

    async def upload(
        self,
        store_id: str,
        path: str,
        data: bytes,
        media_type: MediaType,
        *,
        replace: bool,
    ) -> dict[str, Any]:
        """Extract, split and index an uploaded document."""
        await self._db.run(lambda c: get_store(c, store_id))
        try:
            extracted = await asyncio.to_thread(extract, data, media_type)
        except ExtractError as err:
            raise _error(422, "extract_failed", str(err)) from err
        result = await self._db.run(
            lambda c: _save_document(c, store_id, path, extracted, replace=replace)
        )
        self._changed("document_changed", store_id, {"path": result["path"]})
        return result

    async def items_for_index(self, store_ids: Sequence[str]) -> dict[str, list[str]]:
        """The item paths of each store, sorted by name (for the turn index)."""

        def rows(conn: sqlite3.Connection) -> dict[str, list[str]]:
            found = conn.execute(
                "SELECT store_id, path FROM knowledge_items WHERE store_id IN "
                "(SELECT value FROM json_each(?)) ORDER BY path COLLATE NOCASE",
                (json.dumps(list(store_ids)),),
            ).fetchall()
            result: dict[str, list[str]] = {s: [] for s in store_ids}
            for r in found:
                result[r["store_id"]].append(r["path"])
            return result

        return await self._db.run(rows)

    async def main_files(self, store_ids: Sequence[str]) -> dict[str, str]:
        """The ``MAIN.md`` text of each store that has one."""

        def rows(conn: sqlite3.Connection) -> dict[str, str]:
            found = conn.execute(
                "SELECT store_id, content FROM knowledge_items WHERE kind = 'file' "
                "AND path = ? COLLATE NOCASE AND store_id IN "
                "(SELECT value FROM json_each(?))",
                (MAIN_FILE, json.dumps(list(store_ids))),
            ).fetchall()
            return {r["store_id"]: str(r["content"]) for r in found}

        return await self._db.run(rows)


def _check_name(name: str) -> None:
    if not STORE_NAME.match(name):
        raise _error(
            422,
            "invalid_name",
            "a store name has 1 to 64 letters, digits, '_' or '-'",
        )


def _check_limit(limit: int) -> None:
    if not MIN_SIZE_LIMIT <= limit <= MAX_SIZE_LIMIT:
        raise _error(
            422, "invalid_size_limit", "the size limit is 1000 to 1000000000 bytes"
        )


def _check_free_name(conn: sqlite3.Connection, name: str, store_id: str | None) -> None:
    row = conn.execute(
        "SELECT id FROM knowledge_stores WHERE name = ? COLLATE NOCASE", (name,)
    ).fetchone()
    if row is not None and row["id"] != store_id:
        raise _error(409, "name_conflict", f"a store named {name!r} exists")
