"""The knowledge component: stores, the search index and the embedder.

``default_components()`` creates the component before the app starts and
gives ``component.tools_store`` and ``component.entity_search`` to
``ToolsComponent``. Both forward to the running service. Before the start,
the knowledge tools return ``unavailable``, and ``ha.find_entities`` uses its
own word match.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from aiohttp import web

from hagent_app.agents.definition import AgentDefinition
from hagent_app.http.errors import ApiError
from hagent_app.http.events import ChangeEvents
from hagent_app.knowledge import vectors
from hagent_app.knowledge.config import KnowledgeConfig
from hagent_app.knowledge.context import TurnContext, build_turn_context
from hagent_app.knowledge.embedder import Embedder, EmbeddingModel, parse_provider
from hagent_app.knowledge.entities import EntityCatalog
from hagent_app.knowledge.history import HistoryIndex
from hagent_app.knowledge.index import SearchIndex
from hagent_app.knowledge.service import KnowledgeService
from hagent_app.providers.embeddings import EmbeddingClient
from hagent_app.storage import Database, transaction
from hagent_app.tools.base import ToolUnavailableError
from hagent_app.tools.registry import RegistrySnapshot
from hagent_app.tools.stores import WriteMode

if TYPE_CHECKING:
    from hagent_app.app import AppContext
    from hagent_app.providers import ProvidersComponent
    from hagent_app.tools import ToolsComponent

_LOG = logging.getLogger(__name__)
_SETTING = "embedding_model"


class KnowledgeComponent:
    """Owns ``KnowledgeService``, ``HistoryIndex`` and ``EntityCatalog``."""

    name = "knowledge"

    def __init__(
        self,
        events: ChangeEvents | None = None,
        *,
        providers: ProvidersComponent | None = None,
        tools: ToolsComponent | None = None,
        embedding_client: EmbeddingClient | None = None,
    ) -> None:
        self._events = events
        self._providers = providers
        self._tools = tools
        self._client_override = embedding_client
        self._db: Database | None = None
        self._service: KnowledgeService | None = None
        self.config = KnowledgeConfig()
        self.index: SearchIndex | None = None
        self.embedder: Embedder | None = None
        self.history: HistoryIndex | None = None
        self.catalog: EntityCatalog | None = None
        self.tools_store = _ToolsStore(self)
        self.entity_search = _EntitySearch(self)

    def service(self) -> KnowledgeService:
        """The running service. Raise ``ApiError`` 503 before start."""
        if self._service is None:
            raise ApiError(503, "not_ready", "the knowledge component is not running")
        return self._service

    def running_service(self) -> KnowledgeService | None:
        """The running service, or ``None``."""
        return self._service

    def routes(self) -> list[web.RouteDef]:
        """The routes for ``HttpServer.add_routes``."""
        from hagent_app.knowledge.api import KnowledgeApi  # noqa: PLC0415

        return KnowledgeApi(self).routes()

    def _client(self) -> EmbeddingClient | None:
        if self._client_override is not None:
            return self._client_override
        if self._providers is None:
            return None
        try:
            return self._providers.embeddings
        except RuntimeError:
            return None

    async def start(self, ctx: AppContext) -> None:
        """Load sqlite-vec, start the embedder and build the entity catalogue."""
        self.config = config = ctx.config.section("knowledge", KnowledgeConfig)
        self._db = ctx.db
        index = SearchIndex(ctx.db, max_distance=config.max_vector_distance)
        if config.load_sqlite_vec:
            index.vector_reason = await ctx.db.run(vectors.load)
        else:
            index.vector_reason = "sqlite-vec is turned off in the options"
        embedder = Embedder(
            ctx.db,
            index,
            self._client,
            batch_size=config.embed_batch_size,
            retry_seconds=config.embed_retry_seconds,
        )
        embedder.set_model(await self._stored_model())
        embedder.start()
        self.index, self.embedder = index, embedder
        self.history = HistoryIndex(ctx.db, index, embedder.wake)
        self.catalog = EntityCatalog(ctx.db, index, ctx.states, embedder.wake)
        self._service = KnowledgeService(
            ctx.db,
            index,
            events=self._events,
            on_change=embedder.wake,
            default_size_limit=config.default_size_limit,
        )
        registry = self._tools.registry if self._tools is not None else None
        if registry is not None:
            registry.add_reload_listener(self._on_reload)
            last = registry.last()
            if last is not None:
                await self._on_reload(last)

    async def stop(self) -> None:
        """Stop the embedder."""
        if self.embedder is not None:
            await self.embedder.stop()
        self._service = None

    async def _on_reload(self, snapshot: RegistrySnapshot) -> None:
        if self.catalog is None:
            return
        try:
            await self.catalog.rebuild(snapshot)
        except Exception:
            _LOG.exception("cannot build the entity catalogue")

    async def turn_context(self, definition: AgentDefinition) -> TurnContext:
        """The knowledge part of an agent turn, with the configured index size."""
        return await build_turn_context(
            self.service(), definition, index_entries=self.config.index_entries
        )

    # ---------------------------------------------------------- settings

    def _default_model(self) -> EmbeddingModel | None:
        if not self.config.embedding_model:
            return None
        return EmbeddingModel(
            parse_provider(self.config.embedding_provider),
            self.config.embedding_model,
            self.config.embedding_dimensions,
        )

    async def _stored_model(self) -> EmbeddingModel | None:
        db = self._require_db()

        def read(conn: sqlite3.Connection) -> str | None:
            row = conn.execute(
                "SELECT value FROM knowledge_settings WHERE key = ?", (_SETTING,)
            ).fetchone()
            return None if row is None else str(row[0])

        raw = await db.run(read)
        if raw is None:
            return self._default_model()
        data = json.loads(raw)
        if data is None:
            return None
        return EmbeddingModel(
            parse_provider(str(data["provider"])),
            str(data["model"]),
            data.get("dimensions"),
        )

    async def set_embedding_model(self, model: EmbeddingModel | None) -> None:
        """Select the embedding model. A change starts a full re-index."""
        db = self._require_db()
        value = json.dumps(model.to_data() if model else None)

        def write(conn: sqlite3.Connection) -> None:
            with transaction(conn):
                conn.execute(
                    "INSERT INTO knowledge_settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                    (_SETTING, value),
                )

        await db.run(write)
        if self.embedder is not None:
            self.embedder.set_model(model)
        if self._events is not None:
            self._events.publish("knowledge", "settings_changed", "settings", {})

    def _require_db(self) -> Database:
        if self._db is None:
            raise ApiError(503, "not_ready", "the knowledge component is not running")
        return self._db

    async def status(self) -> dict[str, Any]:
        """The state of the index, for the status sensor and the UI."""
        index, embedder = self.index, self.embedder
        if index is None or embedder is None:
            raise ApiError(503, "not_ready", "the knowledge component is not running")
        model = embedder.model
        reason = index.vector_reason
        if reason is None and model is None:
            reason = "no embedding model is set"
        if reason is None and embedder.last_error:
            reason = f"the embedding provider failed: {embedder.last_error}"
        return {
            "search_mode": "hybrid" if reason is None else "keyword",
            "fallback_reason": reason,
            "sqlite_vec": index.vectors_loaded,
            "embedding": model.to_data() if model else None,
            "pending_embeddings": await embedder.pending(),
            "last_error": embedder.last_error,
            "entity_catalog": self.catalog.size if self.catalog else 0,
        }


def _running(component: KnowledgeComponent) -> KnowledgeService:
    service = component.running_service()
    if service is None:
        msg = "knowledge stores are not available yet"
        raise ToolUnavailableError(msg)
    return service


class _ToolsStore:
    """The ``KnowledgeStore`` that the tool executor gets at build time."""

    def __init__(self, component: KnowledgeComponent) -> None:
        self._component = component

    async def describe(self, store_ids: Sequence[str]) -> list[dict[str, Any]]:
        return await _running(self._component).describe(store_ids)

    async def list_items(self, store_id: str) -> list[dict[str, Any]]:
        return await _running(self._component).list_items(store_id)

    async def read(
        self, store_id: str, path: str, section: str | None
    ) -> dict[str, Any]:
        return await _running(self._component).read(store_id, path, section)

    async def search(
        self, store_ids: Sequence[str], query: str, limit: int
    ) -> list[dict[str, Any]]:
        return await _running(self._component).search(store_ids, query, limit)

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
        return await _running(self._component).write(
            store_id,
            path,
            mode,
            content,
            old_text=old_text,
            base_version=base_version,
            actor_agent_id=actor_agent_id,
        )

    async def delete_file(
        self, store_id: str, path: str, *, base_version: int | None, actor_agent_id: str
    ) -> None:
        await _running(self._component).delete_file(
            store_id, path, base_version=base_version, actor_agent_id=actor_agent_id
        )


class _EntitySearch:
    """The ``EntitySearch`` that the tool executor gets at build time."""

    def __init__(self, component: KnowledgeComponent) -> None:
        self._component = component

    async def rank_entities(
        self, query: str, entity_ids: Sequence[str], limit: int
    ) -> list[str]:
        catalog = self._component.catalog
        if catalog is None:
            msg = "the entity catalogue is not running"
            raise RuntimeError(msg)
        return await catalog.rank_entities(query, entity_ids, limit)
