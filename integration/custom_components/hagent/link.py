"""The client side of the integration link (see docs/link.md).

``LinkClient`` keeps one WebSocket connection to the hagent app. It
connects again with back-off after each failure. When the connection drops,
every command that waits for a response fails at once with
``LinkUnavailableError``.

The client does not import HA. The integration gives it callbacks, so that
the tests can drive it without HA.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable, Mapping
from typing import Any

import aiohttp

from . import const
from .const import (
    LINK_PATH,
    MAX_MESSAGE_BYTES,
    PROTOCOL_VERSION,
)

_LOGGER = logging.getLogger(__name__)

HELLO_TIMEOUT = 10.0


class LinkError(Exception):
    """A link failure."""


class LinkUnavailableError(LinkError):
    """The app is not reachable, or the connection dropped."""


class LinkAuthError(LinkError):
    """The app refused the secret."""


class LinkProtocolError(LinkError):
    """The app speaks another protocol version."""


class LinkCommandError(LinkError):
    """The app answered a command with an error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


type Message = dict[str, Any]


def link_url(host: str, port: int) -> str:
    """The WebSocket URL of the link."""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"ws://{host}:{port}{LINK_PATH}"


async def open_link(
    session: aiohttp.ClientSession,
    url: str,
    secret: str,
    *,
    heartbeat: float | None = 30,
) -> tuple[aiohttp.ClientWebSocketResponse, Message]:
    """Connect and read the ``hello`` message. Raise a ``LinkError``.

    A one-time check (the config flow) passes ``heartbeat=None``.
    """
    try:
        ws = await session.ws_connect(
            url,
            headers={"Authorization": f"Bearer {secret}"},
            heartbeat=heartbeat,
            max_msg_size=MAX_MESSAGE_BYTES,
            timeout=aiohttp.ClientWSTimeout(ws_close=10),
        )
    except aiohttp.WSServerHandshakeError as err:
        if err.status == 401:  # noqa: PLR2004 - HTTP unauthorized
            msg = "the app refused the secret"
            raise LinkAuthError(msg) from err
        msg = f"the app refused the link: {err.status}"
        raise LinkUnavailableError(msg) from err
    except (aiohttp.ClientError, OSError, TimeoutError) as err:
        msg = f"cannot reach the app: {err}"
        raise LinkUnavailableError(msg) from err
    try:
        frame = await ws.receive(timeout=HELLO_TIMEOUT)
        hello = json.loads(frame.data) if frame.type == aiohttp.WSMsgType.TEXT else None
    except (TimeoutError, ValueError) as err:
        await ws.close()
        msg = "the app sent no hello"
        raise LinkUnavailableError(msg) from err
    if not isinstance(hello, dict) or hello.get("type") != "hello":
        await ws.close()
        msg = "the app sent no hello"
        raise LinkUnavailableError(msg)
    if hello.get("protocol") != PROTOCOL_VERSION:
        await ws.close()
        msg_text = f"the app speaks link protocol {hello.get('protocol')!r}"
        raise LinkProtocolError(msg_text)
    return ws, hello


class LinkClient:
    """One link connection with reconnects.

    Callbacks (plain functions, called on the event loop):

    - ``on_hello(hello)``: after each connect, with the full agent list.
    - ``on_message(message)``: each ``agent``, ``agent_removed`` and
      ``result`` message.
    - ``on_connection(connected)``: when the link state changes.
    """

    def __init__(  # noqa: PLR0913
        self,
        session: aiohttp.ClientSession,
        url: str,
        secret: str,
        *,
        on_hello: Callable[[Message], None],
        on_message: Callable[[Message], None],
        on_connection: Callable[[bool], None],
        backoff_initial: float | None = None,
        backoff_max: float | None = None,
    ) -> None:
        self._session = session
        self._url = url
        self._secret = secret
        self._on_hello = on_hello
        self._on_message = on_message
        self._on_connection = on_connection
        self._backoff_initial = (
            const.BACKOFF_INITIAL if backoff_initial is None else backoff_initial
        )
        self._backoff_max = const.BACKOFF_MAX if backoff_max is None else backoff_max
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._next_id = 1
        self._stopped = False
        self.last_error: str | None = None

    @property
    def stopped(self) -> bool:
        """True after ``stop``."""
        return self._stopped

    @property
    def connected(self) -> bool:
        """True while the link is open."""
        return self._ws is not None and not self._ws.closed

    async def run(self) -> None:
        """Connect, handle messages, and connect again until ``stop``."""
        delay = self._backoff_initial
        while not self._stopped:
            try:
                ws, hello = await open_link(self._session, self._url, self._secret)
            except LinkError as err:
                if str(err) != self.last_error:
                    _LOGGER.warning("hagent link: %s", err)
                    self.last_error = str(err)
                    # The link stays down; the state shows the new error.
                    self._on_connection(False)
            else:
                self.last_error = None
                delay = self._backoff_initial
                await self._serve(ws, hello)
            if self.stopped:
                break
            await asyncio.sleep(delay)
            delay = min(delay * 2, self._backoff_max)

    async def _serve(self, ws: aiohttp.ClientWebSocketResponse, hello: Message) -> None:
        self._ws = ws
        _LOGGER.info("hagent link connected")
        try:
            self._on_connection(True)
            self._on_hello(hello)
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    self._handle(msg.data)
                elif msg.type in {aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE}:
                    break
        except Exception:
            _LOGGER.exception("hagent link failed")
        finally:
            self._ws = None
            self._fail_pending("the link to the hagent app dropped")
            with contextlib.suppress(Exception):
                await ws.close()
            if not self._stopped:
                _LOGGER.warning("hagent link disconnected")
            self._on_connection(False)

    def _handle(self, text: str) -> None:
        try:
            msg = json.loads(text)
        except ValueError:
            _LOGGER.warning("hagent link: a message is not JSON")
            return
        if not isinstance(msg, dict):
            return
        if msg.get("type") == "response":
            msg_id = msg.get("id")
            future = (
                self._pending.pop(msg_id, None) if isinstance(msg_id, int) else None
            )
            if future is None or future.done():
                if msg.get("id") is None and not msg.get("success"):
                    _LOGGER.warning("hagent link: %s", msg.get("error"))
                return
            if msg.get("success"):
                future.set_result(msg.get("data"))
            else:
                error = msg.get("error") or {}
                future.set_exception(
                    LinkCommandError(
                        str(error.get("code", "error")), str(error.get("message", ""))
                    )
                )
            return
        try:
            self._on_message(msg)
        except Exception:
            _LOGGER.exception("hagent link: a message handler failed")

    def _fail_pending(self, reason: str) -> None:
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(LinkUnavailableError(reason))

    async def call(
        self,
        kind: str,
        fields: Mapping[str, Any],
        timeout: float,  # noqa: ASYNC109 - a response deadline, not a cancel scope
    ) -> Any:
        """Send one command and wait for its response data.

        Raise ``LinkUnavailableError`` if the link is down or drops, or if no
        response arrives in ``timeout`` seconds. Raise ``LinkCommandError``
        if the app answers with an error.
        """
        ws = self._ws
        if ws is None or ws.closed:
            msg = "the hagent app is not connected"
            raise LinkUnavailableError(msg)
        msg_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future
        try:
            await ws.send_str(json.dumps({**fields, "type": kind, "id": msg_id}))
            async with asyncio.timeout(timeout):
                return await future
        except TimeoutError as err:
            msg = "the hagent app did not answer in time"
            raise LinkUnavailableError(msg) from err
        except (ConnectionError, RuntimeError, aiohttp.ClientError) as err:
            msg = f"cannot send to the hagent app: {err}"
            raise LinkUnavailableError(msg) from err
        finally:
            self._pending.pop(msg_id, None)

    async def stop(self) -> None:
        """Close the connection and stop the reconnect loop."""
        self._stopped = True
        ws = self._ws
        if ws is not None:
            await ws.close()
        self._fail_pending("the hagent integration stopped")
