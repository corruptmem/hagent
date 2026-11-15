"""The management API routes for knowledge stores. ``docs/api.md`` describes them."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from aiohttp import web

from hagent_app.http.errors import ApiError, read_json
from hagent_app.knowledge.embedder import EmbeddingModel, parse_provider
from hagent_app.knowledge.index import hit_json
from hagent_app.knowledge.service import KnowledgeError, KnowledgeService
from hagent_app.knowledge.text import ExtractError, media_type_of, safe_name

if TYPE_CHECKING:
    from hagent_app.knowledge.component import KnowledgeComponent

type Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

_BASE = "/api/knowledge"
_STORE = _BASE + "/stores/{store_id:[a-f0-9]{32}}"
_PATH = "{path:.+}"
_PATH_CHARS = re.compile(r"^[A-Za-z0-9_\-./ ]{1,255}$")
_READ_CHUNK = 64 * 1024


def check_path(value: str) -> str:
    """A store path: safe characters, no empty, ``.`` or ``..`` parts."""
    parts = value.split("/")
    if not _PATH_CHARS.match(value) or any(p.strip() in {"", ".", ".."} for p in parts):
        raise ApiError(
            422,
            "invalid_path",
            "a path uses letters, digits, '_', '-', '.', ' ' and '/', "
            "with no empty, '.' or '..' parts",
        )
    return value


def _object(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ApiError(400, "invalid_body", "the body must be a JSON object")
    return body


def _opt_int(body: dict[str, Any], key: str) -> int | None:
    value = body.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApiError(400, "invalid_body", f"{key} must be an integer")
    return value


def _opt_str(body: dict[str, Any], key: str) -> str | None:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ApiError(400, "invalid_body", f"{key} must be a string")
    return value


def _query_int(request: web.Request, key: str) -> int | None:
    raw = request.query.get(key)
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError as err:
        raise ApiError(400, "invalid_query", f"{key} must be an integer") from err


class KnowledgeApi:
    """The routes of the knowledge area."""

    def __init__(self, component: KnowledgeComponent) -> None:
        self._component = component

    def _service(self) -> KnowledgeService:
        return self._component.service()

    def routes(self) -> list[web.RouteDef]:
        """The route table."""
        h = self._wrap
        return [
            web.get(_BASE + "/stores", h(self.list_stores)),
            web.post(_BASE + "/stores", h(self.create_store)),
            web.get(_STORE, h(self.get_store)),
            web.patch(_STORE, h(self.update_store)),
            web.delete(_STORE, h(self.delete_store)),
            web.get(_STORE + "/items", h(self.list_items)),
            web.get(_STORE + "/items/" + _PATH, h(self.read_item)),
            web.delete(_STORE + "/items/" + _PATH, h(self.delete_item)),
            web.put(_STORE + "/files/" + _PATH, h(self.put_file)),
            web.get(_STORE + "/history/" + _PATH, h(self.history)),
            web.post(_STORE + "/restore", h(self.restore)),
            web.post(_STORE + "/documents", h(self.upload)),
            web.put(_STORE + "/documents/" + _PATH, h(self.replace_document)),
            web.get(_BASE + "/search", h(self.search)),
            web.get(_BASE + "/status", h(self.status)),
            web.get(_BASE + "/settings", h(self.get_settings)),
            web.put(_BASE + "/settings", h(self.put_settings)),
        ]

    @staticmethod
    def _wrap(handler: Handler) -> Handler:
        async def wrapped(request: web.Request) -> web.StreamResponse:
            try:
                return await handler(request)
            except KnowledgeError as err:
                raise ApiError(err.status, err.code, err.message, err.details) from err

        return wrapped

    # ------------------------------------------------------------ stores

    async def list_stores(self, request: web.Request) -> web.Response:
        return web.json_response({"stores": await self._service().stores()})

    async def create_store(self, request: web.Request) -> web.Response:
        body = _object(await read_json(request))
        name = _opt_str(body, "name")
        if not name:
            raise ApiError(400, "invalid_body", "the body needs a name")
        store = await self._service().create_store(
            name, _opt_str(body, "description") or "", _opt_int(body, "size_limit")
        )
        return web.json_response(store, status=201)

    async def get_store(self, request: web.Request) -> web.Response:
        store_id = request.match_info["store_id"]
        return web.json_response(await self._service().store(store_id))

    async def update_store(self, request: web.Request) -> web.Response:
        body = _object(await read_json(request))
        store = await self._service().update_store(
            request.match_info["store_id"],
            name=_opt_str(body, "name"),
            description=_opt_str(body, "description"),
            size_limit=_opt_int(body, "size_limit"),
        )
        return web.json_response(store)

    async def delete_store(self, request: web.Request) -> web.Response:
        await self._service().delete_store(request.match_info["store_id"])
        return web.json_response({"deleted": request.match_info["store_id"]})

    # ------------------------------------------------------------- items

    async def list_items(self, request: web.Request) -> web.Response:
        items = await self._service().list_items(request.match_info["store_id"])
        return web.json_response({"items": items})

    async def read_item(self, request: web.Request) -> web.Response:
        path = check_path(request.match_info["path"])
        section = request.query.get("section") or None
        item = await self._service().read(request.match_info["store_id"], path, section)
        return web.json_response(item)

    async def delete_item(self, request: web.Request) -> web.Response:
        path = check_path(request.match_info["path"])
        result = await self._service().delete_user_item(
            request.match_info["store_id"], path, _query_int(request, "base_version")
        )
        return web.json_response(result)

    async def put_file(self, request: web.Request) -> web.Response:
        path = check_path(request.match_info["path"])
        body = _object(await read_json(request))
        content = body.get("content")
        if not isinstance(content, str):
            raise ApiError(400, "invalid_body", "the body needs a content string")
        result = await self._service().put_file(
            request.match_info["store_id"],
            path,
            content,
            _opt_int(body, "base_version"),
        )
        return web.json_response(result)

    async def history(self, request: web.Request) -> web.Response:
        path = check_path(request.match_info["path"])
        store_id = request.match_info["store_id"]
        version = _query_int(request, "version")
        if version is not None:
            return web.json_response(
                await self._service().version(store_id, path, version)
            )
        return web.json_response(
            {"versions": await self._service().history(store_id, path)}
        )

    async def restore(self, request: web.Request) -> web.Response:
        body = _object(await read_json(request))
        path = check_path(_opt_str(body, "path") or "")
        version = _opt_int(body, "version")
        if version is None:
            raise ApiError(400, "invalid_body", "the body needs a version")
        result = await self._service().restore(
            request.match_info["store_id"],
            path,
            version,
            _opt_int(body, "base_version"),
        )
        return web.json_response(result)

    # --------------------------------------------------------- documents

    async def _body(self, request: web.Request) -> bytes:
        """Read the raw body with the upload limit (aiohttp's 1 MiB limit
        applies only to ``request.read``)."""
        limit = self._component.config.max_upload_bytes
        if request.content_length is not None and request.content_length > limit:
            raise ApiError(413, "upload_too_large", f"the limit is {limit} bytes")
        chunks: list[bytes] = []
        total = 0
        while chunk := await request.content.read(_READ_CHUNK):
            total += len(chunk)
            if total > limit:
                raise ApiError(413, "upload_too_large", f"the limit is {limit} bytes")
            chunks.append(chunk)
        if not total:
            raise ApiError(400, "invalid_body", "the upload is empty")
        return b"".join(chunks)

    async def _store_document(
        self, request: web.Request, name: str, *, replace: bool
    ) -> web.Response:
        try:
            path = check_path(safe_name(name))
            media = media_type_of(path, request.headers.get("Content-Type"))
        except ExtractError as err:
            raise ApiError(415, "unsupported_document", str(err)) from err
        data = await self._body(request)
        result = await self._service().upload(
            request.match_info["store_id"], path, data, media, replace=replace
        )
        return web.json_response(result, status=200 if replace else 201)

    async def upload(self, request: web.Request) -> web.Response:
        name = request.query.get("name")
        if not name:
            raise ApiError(400, "invalid_query", "the query needs a name")
        return await self._store_document(request, name, replace=False)

    async def replace_document(self, request: web.Request) -> web.Response:
        path = check_path(request.match_info["path"])
        return await self._store_document(request, path, replace=True)

    # ------------------------------------------------- search and settings

    async def search(self, request: web.Request) -> web.Response:
        query = request.query.get("q", "").strip()
        if not query:
            raise ApiError(400, "invalid_query", "the query needs q")
        limit = min(max(_query_int(request, "limit") or 10, 1), 50)
        service = self._service()
        store_ids = request.query.getall("store_id", [])
        if not store_ids:
            store_ids = [s["id"] for s in await service.stores()]
        result = await service.search_stores(store_ids, query, limit)
        return web.json_response(
            {
                "mode": result.mode,
                "fallback_reason": result.fallback_reason,
                "results": [hit_json(h) for h in result.hits],
            }
        )

    async def status(self, request: web.Request) -> web.Response:
        return web.json_response(await self._component.status())

    async def get_settings(self, request: web.Request) -> web.Response:
        status = await self._component.status()
        return web.json_response({"embedding": status["embedding"]})

    async def put_settings(self, request: web.Request) -> web.Response:
        body = _object(await read_json(request))
        if "embedding" not in body:
            raise ApiError(400, "invalid_body", "the body needs embedding")
        raw = body["embedding"]
        model = None
        if raw is not None:
            raw = _object(raw)
            name = _opt_str(raw, "model")
            if not name:
                raise ApiError(400, "invalid_body", "embedding needs a model")
            try:
                provider = parse_provider(_opt_str(raw, "provider") or "")
            except ValueError as err:
                raise ApiError(400, "invalid_body", str(err)) from err
            dims = _opt_int(raw, "dimensions")
            if dims is not None and not 1 <= dims <= 8192:  # noqa: PLR2004
                raise ApiError(400, "invalid_body", "dimensions must be 1 to 8192")
            model = EmbeddingModel(provider, name, dims)
        await self._component.set_embedding_model(model)
        return web.json_response({"embedding": model.to_data() if model else None})
