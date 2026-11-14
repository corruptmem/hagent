"""A WebSocket client for the HA WebSocket API.

Inside an app, the client connects to HA Core through the Supervisor proxy at
``ws://supervisor/core/websocket``. It authenticates with the
``SUPERVISOR_TOKEN`` that the Supervisor supplies.

The client runs a background task that keeps the connection open:

- When the connection closes, the client fails every pending command with
  ``HAConnectionError``, so that no caller waits forever.
- It reconnects with exponential backoff, capped at ``backoff_max`` seconds.
- After each connect, it subscribes again to every registered subscription.
  Then it calls the connect listeners. The state cache uses a connect
  listener to load all states again.

Event handlers are plain functions. The reader task calls them in arrival
order. A handler must be fast and must not block. A handler that needs I/O
must put the event on its own queue or start its own task.

HA sends the ``state_changed`` event of a service call before the call
result (task-2). ``call_service`` returns the result context, so that the
caller can link the events to the call with ``OwnWriteTracker``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import aiohttp

_LOG = logging.getLogger(__name__)

type Event = dict[str, Any]
type EventHandler = Callable[[Event], None]
type Listener = Callable[[], Awaitable[None]]

DEFAULT_TIMEOUT = 30.0
HEARTBEAT = 30.0


class HAError(Exception):
    """The base class of HA client errors."""


class HAConnectionError(HAError):
    """The client has no connection, or the connection closed."""


class HAAuthError(HAError):
    """HA rejected the access token."""


class HACommandError(HAError):
    """HA returned an error result for a command."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ServiceResult:
    """The result of a ``call_service`` command.

    ``context`` is the context that HA created for the call. Its ``id`` is in
    every ``state_changed`` event that the call causes. ``response`` is the
    service response, or ``None`` if the caller did not ask for one.
    """

    context: Mapping[str, Any]
    response: Any

    @property
    def context_id(self) -> str | None:
        """The context ID, or ``None`` if HA did not send one."""
        value = self.context.get("id")
        return value if isinstance(value, str) else None


class Subscription:
    """A registered event subscription. It survives reconnects."""

    def __init__(
        self,
        client: HAClient,
        handler: EventHandler,
        event_type: str | None,
    ) -> None:
        self._client = client
        self.handler = handler
        self.event_type = event_type
        self.ha_id: int | None = None

    async def unsubscribe(self) -> None:
        """Remove the subscription, also from HA if connected."""
        await self._client.unsubscribe(self)


class HAClient:
    """A reconnecting HA WebSocket client.

    Call ``start`` once. The client then connects in the background. Call
    ``stop`` to close it. ``connected`` is true only after authentication and
    resubscription.
    """

    def __init__(
        self,
        url: str,
        token: str,
        *,
        backoff_initial: float = 1.0,
        backoff_max: float = 30.0,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._url = url
        self._token = token
        self._backoff_initial = backoff_initial
        self._backoff_max = backoff_max
        self._timeout = timeout
        self._session: aiohttp.ClientSession | None = None
        self._task: asyncio.Task[None] | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._subs: list[Subscription] = []
        self._by_ha_id: dict[int, Subscription] = {}
        self._connect_listeners: list[Listener] = []
        self._disconnect_listeners: list[Listener] = []
        self._connected = asyncio.Event()
        self.ha_version: str | None = None
        self.connect_count = 0

    @property
    def connected(self) -> bool:
        """True when the client is connected, authenticated and subscribed."""
        return self._connected.is_set()

    async def wait_connected(self) -> None:
        """Wait until the client is connected.

        Wrap the call in ``asyncio.timeout`` to limit the wait.
        """
        await self._connected.wait()

    def add_connect_listener(self, listener: Listener) -> None:
        """Call ``listener`` after each connect, after resubscription."""
        self._connect_listeners.append(listener)

    def add_disconnect_listener(self, listener: Listener) -> None:
        """Call ``listener`` after each disconnect."""
        self._disconnect_listeners.append(listener)

    async def start(self) -> None:
        """Start the background connection task."""
        if self._task is not None:
            return
        self._session = aiohttp.ClientSession()
        self._task = asyncio.create_task(self._run(), name="ha-client")

    async def stop(self) -> None:
        """Close the connection and stop the background task."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._session is not None:
            await self._session.close()
            self._session = None

    # Commands

    async def command(self, message: Mapping[str, Any]) -> Any:
        """Send a command and return the ``result`` field of the reply.

        Raise ``HAConnectionError`` if there is no connection or it closes,
        ``HACommandError`` if HA returns an error, and ``TimeoutError`` if
        no reply arrives in time.
        """
        ws = self._ws
        if ws is None or ws.closed:
            msg = "not connected to HA"
            raise HAConnectionError(msg)
        msg_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future
        try:
            await ws.send_str(json.dumps({**message, "id": msg_id}))
            async with asyncio.timeout(self._timeout):
                return await future
        except (aiohttp.ClientError, ConnectionError) as err:
            msg = f"send failed: {err}"
            raise HAConnectionError(msg) from err
        finally:
            self._pending.pop(msg_id, None)

    async def call_service(
        self,
        domain: str,
        service: str,
        service_data: Mapping[str, Any] | None = None,
        *,
        target: Mapping[str, Any] | None = None,
        return_response: bool = False,
    ) -> ServiceResult:
        """Call an HA service. Return the context and the response.

        This method does no scope check. Agent tools must call HA through
        the tool executor, which checks the scope first.
        """
        message: dict[str, Any] = {
            "type": "call_service",
            "domain": domain,
            "service": service,
        }
        if service_data:
            message["service_data"] = dict(service_data)
        if target:
            message["target"] = dict(target)
        if return_response:
            message["return_response"] = True
        result = await self.command(message) or {}
        return ServiceResult(
            context=result.get("context") or {},
            response=result.get("response"),
        )

    async def get_states(self) -> list[dict[str, Any]]:
        """Return all current entity states."""
        states = await self.command({"type": "get_states"})
        return list(states or [])

    # Subscriptions

    async def subscribe_events(
        self, handler: EventHandler, event_type: str | None = None
    ) -> Subscription:
        """Register an event subscription and return it.

        If the client is connected, it subscribes in HA now. Otherwise it
        subscribes after the next connect. ``event_type=None`` means all
        events.
        """
        sub = Subscription(self, handler, event_type)
        self._subs.append(sub)
        if self._ws is not None and not self._ws.closed:
            await self._send_subscribe(sub)
        return sub

    async def unsubscribe(self, sub: Subscription) -> None:
        """Remove a subscription."""
        if sub in self._subs:
            self._subs.remove(sub)
        ha_id, sub.ha_id = sub.ha_id, None
        if ha_id is None:
            return
        self._by_ha_id.pop(ha_id, None)
        with contextlib.suppress(HAError, TimeoutError):
            await self.command({"type": "unsubscribe_events", "subscription": ha_id})

    async def _send_subscribe(self, sub: Subscription) -> None:
        message: dict[str, Any] = {"type": "subscribe_events"}
        if sub.event_type is not None:
            message["event_type"] = sub.event_type
        # Register the ID before the reply, because HA can send an event
        # before the result of the subscribe command.
        msg_id = self._next_id
        sub.ha_id = msg_id
        self._by_ha_id[msg_id] = sub
        try:
            await self.command(message)
        except BaseException:
            self._by_ha_id.pop(msg_id, None)
            sub.ha_id = None
            raise

    # Connection loop

    async def _run(self) -> None:
        delay = self._backoff_initial
        while True:
            try:
                ws = await self._connect()
            except HAAuthError as err:
                _LOG.warning("HA rejected the token: %s", err)
                delay = self._backoff_max
            except (HAError, aiohttp.ClientError, OSError, TimeoutError) as err:
                _LOG.warning("cannot connect to HA: %s", err)
            else:
                delay = self._backoff_initial
                await self._serve(ws)
                continue
            await asyncio.sleep(delay)
            delay = min(delay * 2, self._backoff_max)

    async def _connect(self) -> aiohttp.ClientWebSocketResponse:
        assert self._session is not None  # noqa: S101 - set in start()
        ws = await self._session.ws_connect(
            self._url,
            heartbeat=HEARTBEAT,
            max_msg_size=0,
            timeout=aiohttp.ClientWSTimeout(ws_close=10.0),
        )
        try:
            async with asyncio.timeout(self._timeout):
                hello = await _receive(ws)
                await ws.send_json({"type": "auth", "access_token": self._token})
                reply = await _receive(ws)
        except BaseException:
            await ws.close()
            raise
        if hello.get("type") != "auth_required" or reply.get("type") != "auth_ok":
            await ws.close()
            if reply.get("type") == "auth_invalid":
                raise HAAuthError(str(reply.get("message", "auth failed")))
            msg = f"unexpected auth messages: {hello.get('type')}, {reply.get('type')}"
            raise HAConnectionError(msg)
        self.ha_version = reply.get("ha_version")
        return ws

    async def _serve(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self._ws = ws
        self._next_id = 1
        self._by_ha_id.clear()
        setup = asyncio.create_task(self._on_connect(ws), name="ha-client-setup")
        try:
            await self._read(ws)
        finally:
            setup.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await setup
            self._ws = None
            self._connected.clear()
            self._fail_pending()
            for sub in self._subs:
                sub.ha_id = None
            self._by_ha_id.clear()
            if not ws.closed:
                await ws.close()
        _LOG.warning("connection to HA closed")
        await self._notify(self._disconnect_listeners)

    async def _on_connect(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        try:
            for sub in list(self._subs):
                # A subscribe_events call during setup subscribes on its own.
                if sub.ha_id is None:
                    await self._send_subscribe(sub)
            self.connect_count += 1
            self._connected.set()
            _LOG.info("connected to HA %s", self.ha_version)
            await self._notify(self._connect_listeners)
        except HAError, TimeoutError:
            _LOG.exception("setup after connect failed; reconnecting")
            await ws.close()

    async def _notify(self, listeners: list[Listener]) -> None:
        for listener in list(listeners):
            try:
                await listener()
            except HAConnectionError as err:
                _LOG.warning("listener lost the connection: %s", err)
            except Exception:
                _LOG.exception("listener failed")

    async def _read(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        async for frame in ws:
            if frame.type is not aiohttp.WSMsgType.TEXT:
                if frame.type is aiohttp.WSMsgType.ERROR:
                    _LOG.warning("WebSocket error: %s", ws.exception())
                    break
                continue
            try:
                data = json.loads(frame.data)
            except json.JSONDecodeError:
                _LOG.warning("HA sent a frame that is not JSON")
                continue
            for message in data if isinstance(data, list) else [data]:
                self._dispatch(message)

    def _dispatch(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        msg_id = message.get("id")
        if kind == "event":
            sub = self._by_ha_id.get(msg_id) if isinstance(msg_id, int) else None
            if sub is None:
                return
            try:
                sub.handler(message.get("event", {}))
            except Exception:
                _LOG.exception("event handler failed")
            return
        if kind != "result" or not isinstance(msg_id, int):
            return
        future = self._pending.get(msg_id)
        if future is None or future.done():
            return
        if message.get("success"):
            future.set_result(message.get("result"))
        else:
            error = message.get("error") or {}
            future.set_exception(
                HACommandError(
                    str(error.get("code", "unknown")),
                    str(error.get("message", "")),
                )
            )

    def _fail_pending(self) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(HAConnectionError("connection to HA closed"))
        self._pending.clear()


async def _receive(ws: aiohttp.ClientWebSocketResponse) -> dict[str, Any]:
    """Receive one JSON object during authentication."""
    frame = await ws.receive()
    if frame.type is not aiohttp.WSMsgType.TEXT:
        msg = f"connection closed during authentication ({frame.type.name})"
        raise HAConnectionError(msg)
    try:
        data = json.loads(frame.data)
    except json.JSONDecodeError as err:
        msg = "HA sent a frame that is not JSON"
        raise HAConnectionError(msg) from err
    if not isinstance(data, dict):
        msg = "HA sent an authentication message that is not an object"
        raise HAConnectionError(msg)
    return data
