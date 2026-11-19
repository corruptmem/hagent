"""Serve the built management UI under the Ingress base path.

The browser opens the UI at ``/api/hassio_ingress/<token>/``. Ingress
removes this prefix and sends it in the ``X-Ingress-Path`` header. The UI
uses only relative URLs. To make them resolve from every page of the UI,
the app adds ``<base href="<X-Ingress-Path>/">`` to ``index.html`` when it
serves the file. The UI router reads its base path from the same element.

Each path that does not start with ``api/`` is a UI path:

- A file in the UI directory: the app sends the file.
- A path with a file extension that does not exist: 404.
- Any other path: ``index.html``, so that a deep link opens the right
  screen in the UI.
"""

from __future__ import annotations

import html
import logging
import re
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from aiohttp import web

from hagent_app.http.errors import ApiError
from hagent_app.ingress.config import IngressConfig

if TYPE_CHECKING:
    from hagent_app.app import AppContext

_LOG = logging.getLogger(__name__)

# Every path except ``api`` and ``api/...``. Other areas register their API
# routes later, so this route must never match them.
UI_ROUTE = r"/{tail:(?!api(?:/|$)).*}"
INGRESS_HEADER = "X-Ingress-Path"
_SAFE_BASE = re.compile(r"(/[A-Za-z0-9._~-]+)*")
_HEAD = re.compile(r"<head[^>]*>", re.IGNORECASE)
_MISSING = """<!doctype html>
<html><head><meta charset="utf-8"><title>hagent</title></head>
<body><h1>hagent</h1><p>The management UI is not built. Run
<code>npm ci &amp;&amp; npm run build</code> in <code>frontend/</code>.</p>
</body></html>
"""


def ingress_base(header: str | None) -> str:
    """The base path for ``<base href>``, always with a final slash.

    The app accepts only a plain path of safe characters. It ignores any
    other value, so that the header cannot inject HTML.
    """
    value = (header or "").rstrip("/")
    if not _SAFE_BASE.fullmatch(value):
        _LOG.warning("ignored a header %s that is not a plain path", INGRESS_HEADER)
        value = ""
    return value + "/"


def inject_base(index_html: str, base: str) -> str:
    """Add a ``<base>`` element as the first child of ``<head>``."""
    element = f'<base href="{html.escape(base, quote=True)}">'
    match = _HEAD.search(index_html)
    if match is None:
        return element + index_html
    return index_html[: match.end()] + element + index_html[match.end() :]


class UiComponent:
    """A component that serves the UI files."""

    name = "ui"

    def __init__(self) -> None:
        self._root: Path | None = None

    def routes(self) -> list[web.RouteDef]:
        """The routes for ``HttpServer.add_routes``."""
        return [web.get(UI_ROUTE, self._serve)]

    async def start(self, ctx: AppContext) -> None:
        """Read the UI directory from the ``ingress`` options."""
        root = ctx.config.section("ingress", IngressConfig).ui_path().resolve()
        if not (root / "index.html").is_file():
            _LOG.warning("no UI build in %s", root)
        self._root = root

    async def stop(self) -> None:
        """Stop to serve files."""
        self._root = None

    async def _serve(self, request: web.Request) -> web.StreamResponse:
        root = self._root
        if root is None:
            raise ApiError(503, "not_ready", "the UI component is not running")
        tail = request.match_info["tail"]
        parts = PurePosixPath(tail).parts
        if ".." in parts or "\\" in tail:
            raise ApiError(404, "not_found", "the path does not exist")
        if parts:
            path = (root / tail).resolve()
            if not path.is_relative_to(root):
                raise ApiError(404, "not_found", "the path does not exist")
            if path == root / "index.html":
                return self._index(root, request)
            if path.is_file():
                return web.FileResponse(path, headers=_cache_headers(path))
            if PurePosixPath(tail).suffix:
                raise ApiError(404, "not_found", "the path does not exist")
        return self._index(root, request)

    @staticmethod
    def _index(root: Path, request: web.Request) -> web.Response:
        index = root / "index.html"
        try:
            text = index.read_text(encoding="utf-8")
        except FileNotFoundError:
            text = _MISSING
        base = ingress_base(request.headers.get(INGRESS_HEADER))
        return web.Response(
            text=inject_base(text, base),
            content_type="text/html",
            headers={"Cache-Control": "no-cache"},
        )


def _cache_headers(path: Path) -> dict[str, str]:
    # Vite puts a content hash in each file name under ``assets/``.
    if path.parent.name == "assets":
        return {"Cache-Control": "public, max-age=31536000, immutable"}
    return {"Cache-Control": "no-cache"}
