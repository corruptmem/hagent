"""The HTTP server of the app: the management API and, later, the UI.

``HttpServer`` is a component. It serves one aiohttp application on the
configured port. Other areas add their routes with ``add_routes`` and their
middleware with ``add_middleware``. They must do this before the server
starts, because aiohttp freezes the routes at start. The normal place is
``default_components()`` in ``app.py``::

    http = HttpServer()
    agents = AgentsComponent(http.events)
    http.add_routes(agents.routes())
    return [agents, http]

Keep ``HttpServer`` last in the component list. ``App`` then starts it after
the components that serve its routes, and stops it before them.

Middleware order: the JSON error middleware runs first. It converts an
``ApiError`` from any later middleware or handler to a JSON response. The
middlewares from ``add_middleware`` run next, in the order of the calls, and
then the handler. Task-18 adds the Ingress source-address check (design
section 6.1) with ``add_middleware``, so that the check runs before every
route.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING

from aiohttp import WSMsgType, web
from aiohttp.typedefs import Handler, Middleware

from hagent_app.http.errors import ApiError, error_response
from hagent_app.http.events import ChangeEvent, ChangeEvents

if TYPE_CHECKING:
    from hagent_app.app import AppContext

_LOG = logging.getLogger(__name__)

# The app listens on the internal Supervisor network. Ingress sends requests
# from the Supervisor address, so the server must accept them on every
# interface. Task-18 limits the source address with a middleware.
DEFAULT_HOST = "0.0.0.0"  # noqa: S104
EVENTS_PATH = "/api/events"
EVENT_QUEUE_SIZE = 256


@web.middleware
async def _error_middleware(
    request: web.Request, handler: Handler
) -> web.StreamResponse:
    try:
        return await handler(request)
    except ApiError as err:
        return error_response(err.status, err.code, err.message, err.details)
    except web.HTTPException as err:
        if err.status < 400:  # noqa: PLR2004 - redirects pass through
            raise
        code = err.reason.lower().replace(" ", "_")
        return error_response(err.status, code, err.reason)


class HttpServer:
    """The aiohttp server component."""

    name = "http"

    def __init__(self, *, host: str = DEFAULT_HOST, port: int | None = None) -> None:
        self.events = ChangeEvents()
        self._host = host
        self._port = port
        self._routes: list[web.AbstractRouteDef] = []
        self._middlewares: list[Middleware] = []
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._clients: set[asyncio.Queue[ChangeEvent | None]] = set()
        self._unsubscribe = self.events.subscribe(self._fan_out)
        self.add_routes([web.get(EVENTS_PATH, self._events_ws)])

    # ------------------------------------------------------------ hooks

    def add_routes(self, routes: Iterable[web.AbstractRouteDef]) -> None:
        """Add routes. Call this before the server starts."""
        self._check_not_started()
        self._routes.extend(routes)

    def add_middleware(self, middleware: Middleware) -> None:
        """Add a middleware that runs before the routes. Call it before start."""
        self._check_not_started()
        self._middlewares.append(middleware)

    def _check_not_started(self) -> None:
        if self._runner is not None:
            msg = "add routes and middleware before the HTTP server starts"
            raise RuntimeError(msg)

    # -------------------------------------------------------- lifecycle

    def make_app(self) -> web.Application:
        """Build the aiohttp application with every route and middleware."""
        app = web.Application(middlewares=[_error_middleware, *self._middlewares])
        app.add_routes(self._routes)
        app.on_shutdown.append(self._close_clients)
        return app

    async def start(self, ctx: AppContext) -> None:
        """Start to serve on the configured port."""
        port = ctx.config.port if self._port is None else self._port
        runner = web.AppRunner(self.make_app(), access_log=None)
        await runner.setup()
        self._runner = runner
        self._site = web.TCPSite(runner, self._host, port)
        await self._site.start()
        _LOG.info("HTTP server listens on port %s", self.port)

    async def stop(self) -> None:
        """Close the event connections and stop the server."""
        if self._runner is not None:
            await self._runner.cleanup()
        self._runner = None
        self._site = None

    @property
    def port(self) -> int | None:
        """The bound port, also when the configured port is 0."""
        if self._runner is None:
            return None
        for address in self._runner.addresses:
            if isinstance(address, tuple):
                return int(address[1])
        return None

    # ----------------------------------------------------------- events

    def _fan_out(self, event: ChangeEvent) -> None:
        for queue in list(self._clients):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A slow client loses its connection, not other clients'
                # events. It loads the data again when it reconnects.
                self._clients.discard(queue)
                with contextlib.suppress(asyncio.QueueFull):
                    queue.get_nowait()
                    queue.put_nowait(None)

    async def _close_clients(self, _app: web.Application) -> None:
        for queue in list(self._clients):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(None)
        self._clients.clear()

    async def _events_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        queue: asyncio.Queue[ChangeEvent | None] = asyncio.Queue(EVENT_QUEUE_SIZE)
        self._clients.add(queue)
        sender = asyncio.create_task(self._send_events(ws, queue))
        try:
            await ws.send_json({"topic": "events", "action": "ready", "id": ""})
            async for msg in ws:
                if msg.type == WSMsgType.ERROR:
                    break
        finally:
            self._clients.discard(queue)
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sender
            await ws.close()
        return ws

    @staticmethod
    async def _send_events(
        ws: web.WebSocketResponse, queue: asyncio.Queue[ChangeEvent | None]
    ) -> None:
        while (event := await queue.get()) is not None:
            await ws.send_json(event.to_json())
        await ws.close()
