"""The integration link: a WebSocket server for the HA integration.

The HA integration (``integration/custom_components/hagent``) runs inside
HA Core. It connects to ``LINK_PATH`` from HA Core, not through Ingress, so
the Ingress guard exempts exactly that path. The link authenticates each
connection itself: the upgrade request must carry the shared secret in the
``Authorization`` header. Otherwise the app answers 401 and does not open the
WebSocket. ``docs/link.md`` describes the messages.

Terms:

- **Agent snapshot**: the data of one agent that the integration shows on
  the agent device (name, status, last result, cost today, and so on).
- **Delivered result**: a result that the app returned to a waiting
  ``invoke`` command. The integration fires ``hagent_result`` only for
  results that it did not receive as response data.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import socket
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import aiohttp
from aiohttp import WSMsgType, web

from hagent_app.agents import Agent, AgentReader, AgentService
from hagent_app.agents.models import AgentNotFoundError
from hagent_app.config import SUPERVISOR_WS_URL
from hagent_app.http.errors import ApiError
from hagent_app.http.events import ChangeEvent, ChangeEvents
from hagent_app.link.config import LinkConfig
from hagent_app.link.protocol import (
    INVALID_MESSAGE,
    LINK_PATH,
    MAX_MESSAGE_BYTES,
    NOT_FOUND,
    PROTOCOL_VERSION,
    STATUS_BUDGET,
    STATUS_ERROR,
    STATUS_IDLE,
    STATUS_OFF,
    STATUS_RUNNING,
    TIMEOUT,
    UNKNOWN_COMMAND,
    LinkError,
    agent_snapshot,
    error_response,
    last_result_data,
    parse_invoke,
    response,
)
from hagent_app.link.secret import load_or_create_secret, secret_matches
from hagent_app.router.errors import RouterNotRunningError
from hagent_app.router.models import NOT_RUNNING, InvocationResult

if TYPE_CHECKING:
    from hagent_app.app import AppContext
    from hagent_app.router import RouterComponent

_LOG = logging.getLogger(__name__)

INFO_PATH = "/api/integration"
DISCOVERY_SERVICE = "hagent"
# Outgoing messages per connection. A slow client loses its connection; it
# loads every agent again when it reconnects.
SEND_QUEUE_SIZE = 1024

type CostSource = Callable[[str], Awaitable[tuple[Decimal, Decimal]]]


class _Connection:
    """One authenticated integration connection."""

    def __init__(self, ws: web.WebSocketResponse) -> None:
        self.ws = ws
        self.queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(
            SEND_QUEUE_SIZE
        )
        self.tasks: set[asyncio.Task[None]] = set()

    def send(self, message: dict[str, Any]) -> None:
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            _LOG.warning("the integration link is too slow; closing it")
            self.close()

    def close(self) -> None:
        with contextlib.suppress(asyncio.QueueEmpty):
            while True:
                self.queue.get_nowait()
        self.queue.put_nowait(None)


class LinkComponent:
    """The integration link. List it after ``router`` in ``default_components``.

    ``exempt_paths`` is the value for ``IngressGuard(exempt_paths=...)``. It
    holds only the exact link path, not its subpaths.
    """

    name = "link"
    exempt_paths = (LINK_PATH,)

    def __init__(
        self,
        events: ChangeEvents,
        router: RouterComponent,
        *,
        cost: CostSource | None = None,
    ) -> None:
        self._events = events
        self._router = router
        self._cost = cost
        self._ctx: AppContext | None = None
        self._config = LinkConfig()
        self._secret = ""
        self._reader: AgentReader | None = None
        self._service: AgentService | None = None
        self._connections: set[_Connection] = set()
        self._dirty: set[str] = set()
        self._wake = asyncio.Event()
        self._last: dict[str, dict[str, Any]] = {}
        # Invocations that an invoke command waits for, and invocations
        # whose caller got an error (on_timeout: cancel).
        self._waiting: set[str] = set()
        self._suppressed: set[str] = set()
        self._tasks: list[asyncio.Task[None]] = []
        self._unsubscribe: Callable[[], None] | None = None
        router.add_result_listener(self._on_result)
        router.add_activity_listener(self._on_activity)

    # ------------------------------------------------------------ lifecycle

    async def start(self, ctx: AppContext) -> None:
        """Load the secret, then accept connections."""
        self._config = ctx.config.section("link", LinkConfig)
        self._secret = await asyncio.to_thread(
            load_or_create_secret, ctx.config.data_dir
        )
        self._ctx = ctx
        self._reader = AgentReader(ctx.db)
        self._service = AgentService(ctx.db, self._events)
        self._wake = asyncio.Event()
        self._unsubscribe = self._events.subscribe(self._on_change)
        self._tasks = [
            asyncio.create_task(self._refresher(), name="link-refresh"),
            asyncio.create_task(self._ticker(), name="link-tick"),
        ]
        if self._config.discovery and ctx.config.ha_url == SUPERVISOR_WS_URL:
            self._tasks.append(
                asyncio.create_task(self._discovery(), name="link-discovery")
            )

    async def stop(self) -> None:
        """Close every connection and stop the background tasks."""
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        for conn in list(self._connections):
            conn.close()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        self._ctx = None

    @property
    def secret(self) -> str:
        """The shared secret. Empty before start."""
        return self._secret

    def routes(self) -> list[web.RouteDef]:
        """The link WebSocket and the guarded set-up information route."""
        return [web.get(LINK_PATH, self._link_ws), web.get(INFO_PATH, self._info)]

    # ------------------------------------------------------------- routes

    async def _info(self, _request: web.Request) -> web.Response:
        """``GET api/integration``: what the integration set-up needs.

        This route is not under the exempt link path, so the Ingress guard
        checks it like every other route.
        """
        if self._ctx is None:
            raise ApiError(503, "not_ready", "the link is not running")
        return web.json_response(
            {
                "host": self._host(),
                "port": self._ctx.config.port,
                "path": LINK_PATH,
                "secret": self._secret,
                "protocol": PROTOCOL_VERSION,
                "connections": len(self._connections),
            }
        )

    async def _link_ws(self, request: web.Request) -> web.StreamResponse:
        if self._ctx is None:
            raise ApiError(503, "not_ready", "the link is not running")
        if not secret_matches(self._secret, request.headers.get("Authorization")):
            _LOG.warning("refused an integration link from %s", request.remote)
            raise ApiError(401, "unauthorized", "the link secret is not valid")
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=MAX_MESSAGE_BYTES)
        await ws.prepare(request)
        conn = _Connection(ws)
        sender = asyncio.create_task(self._sender(conn))
        try:
            conn.send(await self._hello())
            self._connections.add(conn)
            _LOG.info("the integration link connected from %s", request.remote)
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    self._dispatch(conn, msg.data)
                elif msg.type == WSMsgType.ERROR:
                    break
        finally:
            self._connections.discard(conn)
            for task in list(conn.tasks):
                task.cancel()
            conn.close()
            with contextlib.suppress(asyncio.CancelledError):
                await sender
            await ws.close()
            _LOG.info("the integration link disconnected")
        return ws

    @staticmethod
    async def _sender(conn: _Connection) -> None:
        while (message := await conn.queue.get()) is not None:
            try:
                await conn.ws.send_str(
                    json.dumps(message, ensure_ascii=False, default=str)
                )
            except ConnectionError, RuntimeError:
                break
        await conn.ws.close()

    # ----------------------------------------------------------- commands

    def _dispatch(self, conn: _Connection, text: str) -> None:
        try:
            msg = json.loads(text)
        except ValueError:
            conn.send(error_response(None, INVALID_MESSAGE, "the message is not JSON"))
            return
        msg_id = msg.get("id") if isinstance(msg, dict) else None
        if not isinstance(msg, dict) or not isinstance(msg_id, int):
            conn.send(
                error_response(None, INVALID_MESSAGE, "a command needs an integer id")
            )
            return
        task = asyncio.create_task(self._command(conn, msg_id, msg))
        conn.tasks.add(task)
        task.add_done_callback(conn.tasks.discard)

    async def _command(
        self, conn: _Connection, msg_id: int, msg: dict[str, Any]
    ) -> None:
        kind = msg.get("type")
        try:
            if kind == "invoke":
                data = await self._invoke(msg)
            elif kind == "cancel":
                data = await self._cancel(msg)
            elif kind == "set_enabled":
                data = await self._set_enabled(msg)
            elif kind == "ping":
                data = {}
            else:
                data = _unknown(kind)
        except LinkError as err:
            conn.send(error_response(msg_id, err.code, err.message))
            return
        except Exception as err:
            _LOG.exception("link command %s failed", kind)
            conn.send(error_response(msg_id, "internal_error", repr(err)))
            return
        conn.send(response(msg_id, data))

    async def _invoke(self, msg: dict[str, Any]) -> dict[str, Any]:
        command = parse_invoke(msg)
        request = command.request
        try:
            handle = await self._router.submit(request)
        except RouterNotRunningError as err:
            raise LinkError(NOT_RUNNING, str(err)) from err
        invocation_id = handle.invocation_id
        if not command.wait:
            if handle.done():
                return (await handle.result()).to_data()
            return {"invocation_id": invocation_id, "status": "queued"}
        self._waiting.add(invocation_id)
        try:
            result = await asyncio.wait_for(handle.result(), command.timeout)
        except TimeoutError:
            on_timeout = command.on_timeout or await self._default_on_timeout(
                request.agent_id
            )
            if on_timeout == "cancel":
                self._suppressed.add(invocation_id)
                await self._router.cancel(invocation_id)
                msg_text = (
                    f"the invocation did not finish in {command.timeout:g} s;"
                    " the app cancelled it"
                )
                raise LinkError(TIMEOUT, msg_text) from None
            return {
                "invocation_id": invocation_id,
                "status": "running",
                "timed_out": True,
            }
        finally:
            self._waiting.discard(invocation_id)
        return result.to_data()

    async def _default_on_timeout(self, agent_id: str) -> str:
        """``cancel`` for agents with outputs, ``continue`` for others (9.2)."""
        agent = await self._agents().get(agent_id)
        if agent is not None and agent.definition.outputs:
            return "cancel"
        return "continue"

    async def _cancel(self, msg: dict[str, Any]) -> dict[str, Any]:
        invocation_id = msg.get("invocation_id")
        if not isinstance(invocation_id, str):
            raise LinkError(INVALID_MESSAGE, "'invocation_id' must be a text")
        if not await self._router.cancel(invocation_id):
            raise LinkError(NOT_FOUND, "no queued or running invocation has this ID")
        return {}

    async def _set_enabled(self, msg: dict[str, Any]) -> dict[str, Any]:
        agent_id = msg.get("agent_id")
        enabled = msg.get("enabled")
        if not isinstance(agent_id, str) or not isinstance(enabled, bool):
            raise LinkError(INVALID_MESSAGE, "set_enabled needs agent_id and enabled")
        if self._service is None:
            raise LinkError(NOT_RUNNING, "the link is not running")
        try:
            agent = await self._service.set_enabled(agent_id, enabled)
        except AgentNotFoundError as err:
            raise LinkError("agent_not_found", err.message) from err
        return {"enabled": agent.enabled}

    # ------------------------------------------------------------ snapshots

    def _agents(self) -> AgentReader:
        if self._reader is None:
            raise LinkError(NOT_RUNNING, "the link is not running")
        return self._reader

    async def _hello(self) -> dict[str, Any]:
        assert self._ctx is not None  # noqa: S101 - checked by the caller
        agents = await self._agents().list_agents()
        return {
            "type": "hello",
            "protocol": PROTOCOL_VERSION,
            "installation_id": self._ctx.installation_id,
            "agents": [await self._snapshot(a) for a in agents],
        }

    async def _snapshot(self, agent: Agent) -> dict[str, Any]:
        running = self._router.running_count(agent.id)
        last = self._last.get(agent.id)
        if last is None:
            last = await self._load_last(agent.id)
            if last is not None:
                self._last[agent.id] = last
        cost: Decimal | None = None
        remaining: Decimal | None = None
        if self._cost is not None:
            try:
                cost, remaining = await self._cost(agent.id)
            except Exception:
                _LOG.debug("no cost data for agent %s", agent.id, exc_info=True)
        if not agent.enabled:
            status = STATUS_OFF
        elif running:
            status = STATUS_RUNNING
        elif remaining is not None and remaining <= 0:
            status = STATUS_BUDGET
        elif last is not None and last.get("status") == "error":
            status = STATUS_ERROR
        else:
            status = STATUS_IDLE
        return agent_snapshot(
            agent,
            status=status,
            running=running,
            cost_today_usd=None if cost is None else float(cost),
            last_result=last,
        )

    async def _load_last(self, agent_id: str) -> dict[str, Any] | None:
        assert self._ctx is not None  # noqa: S101 - set before connections
        row = await self._ctx.db.run(lambda c: _last_row(c, agent_id))
        if row is None:
            return None
        (inv_id, status, path, result, code, error, finished) = row
        value = None if result is None else json.loads(result)
        return last_result_data(
            InvocationResult(
                inv_id,
                agent_id,
                status,
                path=path,
                result=value,
                error_code=code,
                error=error,
            ),
            finished,
        )

    # --------------------------------------------------------------- events

    def _broadcast(self, message: dict[str, Any]) -> None:
        for conn in list(self._connections):
            conn.send(message)

    def _mark(self, agent_id: str) -> None:
        self._dirty.add(agent_id)
        self._wake.set()

    def _on_change(self, event: ChangeEvent) -> None:
        if event.topic == "agents":
            self._mark(event.id)

    def _on_activity(self, agent_id: str, _running: int) -> None:
        self._mark(agent_id)

    def _on_result(self, result: InvocationResult) -> None:
        if result.dry_run:
            return
        finished = datetime.now(UTC).isoformat()
        self._last[result.agent_id] = last_result_data(result, finished)
        delivered = result.invocation_id in self._waiting
        if result.invocation_id in self._suppressed:
            self._suppressed.discard(result.invocation_id)
            delivered = True
        data = result.to_data()
        data["agent_id"] = result.agent_id
        self._broadcast({"type": "result", "delivered": delivered, "result": data})
        self._mark(result.agent_id)

    async def _refresher(self) -> None:
        """Send the snapshot of each changed agent."""
        while True:
            await self._wake.wait()
            self._wake.clear()
            ids, self._dirty = self._dirty, set()
            for agent_id in ids:
                try:
                    agent = await self._agents().get(agent_id)
                    if agent is None:
                        self._last.pop(agent_id, None)
                        self._broadcast({"type": "agent_removed", "agent_id": agent_id})
                    else:
                        snapshot = await self._snapshot(agent)
                        self._broadcast({"type": "agent", "agent": snapshot})
                except Exception:
                    _LOG.exception("could not send the snapshot of %s", agent_id)

    async def _ticker(self) -> None:
        """Refresh every agent at an interval: cost today and budget state."""
        while True:
            await asyncio.sleep(self._config.refresh_seconds)
            if not self._connections:
                continue
            with contextlib.suppress(Exception):
                for agent in await self._agents().list_agents():
                    self._mark(agent.id)

    # ------------------------------------------------------------ discovery

    def _host(self) -> str:
        return self._config.host or socket.gethostname()

    async def _discovery(self) -> None:
        """Send the link address and secret to the Supervisor (hassio discovery).

        HA then offers to set up the integration with these values. The app
        configuration lists ``hagent`` under ``discovery`` (hagent/config.yaml).
        """
        assert self._ctx is not None  # noqa: S101 - started
        payload = {
            "service": DISCOVERY_SERVICE,
            "config": {
                "host": self._host(),
                "port": self._ctx.config.port,
                "secret": self._secret,
            },
        }
        await post_discovery(
            self._config.supervisor_url, self._ctx.config.ha_token, payload
        )


async def post_discovery(
    supervisor_url: str,
    token: str,
    payload: dict[str, Any],
    *,
    attempts: int = 5,
    delay: float = 10.0,
) -> bool:
    """Post a discovery message to the Supervisor. Retry on failure."""
    url = supervisor_url.rstrip("/") + "/discovery"
    headers = {"Authorization": f"Bearer {token}"}
    for attempt in range(attempts):
        try:
            async with (
                aiohttp.ClientSession() as session,
                session.post(url, json=payload, headers=headers) as resp,
            ):
                if resp.status < 300:  # noqa: PLR2004 - 2xx
                    _LOG.info("sent the integration discovery to the Supervisor")
                    return True
                _LOG.warning("the Supervisor refused the discovery: %s", resp.status)
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOG.warning("could not send the discovery: %s", err)
        if attempt + 1 < attempts:
            await asyncio.sleep(delay)
    return False


def _last_row(conn: sqlite3.Connection, agent_id: str) -> tuple[Any, ...] | None:
    row = conn.execute(
        "SELECT id, status, path, result, error_code, error, finished_at"
        " FROM invocations WHERE agent_id = ? AND dry_run = 0"
        " AND finished_at IS NOT NULL ORDER BY finished_at DESC LIMIT 1",
        (agent_id,),
    ).fetchone()
    return None if row is None else tuple(row)


def _unknown(kind: Any) -> dict[str, Any]:
    raise LinkError(UNKNOWN_COMMAND, f"unknown command {kind!r}")
