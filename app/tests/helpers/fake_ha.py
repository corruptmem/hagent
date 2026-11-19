"""A fake HA WebSocket server for tests.

Tests must not need a live HA instance. ``FakeHA`` speaks the parts of the HA
WebSocket API that the app uses:

- authentication (``auth_required``, ``auth``, ``auth_ok`` or
  ``auth_invalid``);
- ``get_states``;
- ``subscribe_events`` and ``unsubscribe_events``;
- ``call_service``. A call to ``turn_on``, ``turn_off`` or ``toggle``
  changes the target entities. Like real HA (task-2), the fake sends the
  ``state_changed`` events before the call result, and both carry the same
  context;
- ``auth/current_user``, from ``FakeHA.user``;
- the registry lists (``config/<kind>_registry/list`` for entity,
  device, area, floor and label), ``config/entity_registry/get`` and
  ``get_services``, from ``FakeHA.registry`` and ``FakeHA.services``.
  ``FakeHA.registry_updated`` sends ``<kind>_registry_updated``;
- ``history/history_during_period`` and
  ``recorder/statistics_during_period``. They return an entry for each
  entity in ``states``, so that a test can see a reply that names more
  entities than the request;
- other commands, through ``FakeHA.handlers``.

``FakeHA.commands`` records every command that a client sends after
authentication. A scope test can assert that an out-of-scope call never
appears there.

``FakeHA.restart`` simulates an HA restart: the fake closes every
connection and refuses new ones until ``FakeHA.up`` runs.

Use the ``fake_ha`` fixture from ``conftest.py``, or start one yourself::

    async with FakeHA() as ha:
        client = HAClient(ha.url, ha.token)
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from functools import partial
from typing import Any, Self

from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer

type Message = dict[str, Any]
type Handler = Callable[["FakeHA", Message], Any]

TOKEN = "test-token"
APP_USER_ID = "f" * 32


def now_iso() -> str:
    """The current time in the HA ISO format."""
    return datetime.now(UTC).isoformat()


def make_context() -> dict[str, Any]:
    """A new HA context with a random ID."""
    return {"id": uuid.uuid4().hex.upper()[:26], "parent_id": None, "user_id": None}


class _Conn:
    def __init__(self, ws: web.WebSocketResponse) -> None:
        self.ws = ws
        self.subs: dict[int, str | None] = {}


class FakeHA:
    """A fake HA Core WebSocket endpoint."""

    def __init__(self, token: str = TOKEN, ha_version: str = "2026.11.0") -> None:
        self.token = token
        self.ha_version = ha_version
        # HA's configured time zone, for get_config (the rule templates).
        self.time_zone = "UTC"
        self.states: dict[str, Message] = {}
        self.commands: list[Message] = []
        self.handlers: dict[str, Handler] = {}
        self.registry: dict[str, list[Message]] = {
            kind: [] for kind in ("entity", "device", "area", "floor", "label")
        }
        self.services: dict[str, dict[str, Message]] = {}
        # The HA user of the client token, for ``auth/current_user``.
        self.user: Message = {"id": APP_USER_ID, "name": "hagent", "is_admin": True}
        self.connections_accepted = 0
        self._conns: list[_Conn] = []
        self._down = False
        self._event_ids = itertools.count(1)
        app = web.Application()
        app.router.add_get("/api/websocket", self._handle)
        self._server = TestServer(app)

    @property
    def url(self) -> str:
        """The WebSocket URL of the fake."""
        return str(self._server.make_url("/api/websocket")).replace("http", "ws", 1)

    @property
    def connection_count(self) -> int:
        """The number of open client connections."""
        return len(self._conns)

    async def start(self) -> None:
        """Start the server on a free local port."""
        await self._server.start_server()

    async def close(self) -> None:
        """Close every connection and stop the server."""
        await self.drop()
        await self._server.close()

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # Control from tests

    def set_state(
        self,
        entity_id: str,
        state: str,
        attributes: Mapping[str, Any] | None = None,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> asyncio.Task[None]:
        """Set a state and send ``state_changed`` to subscribers.

        Like real HA, the fake sends no event when the state and the
        attributes do not change. Return the send task. Await it to be sure
        that the events are sent.
        """
        old = self.states.get(entity_id)
        if (
            old is not None
            and old["state"] == state
            and old["attributes"] == dict(attributes or {})
        ):
            return asyncio.create_task(asyncio.sleep(0))
        stamp = now_iso()
        changed = old is None or old["state"] != state
        new: Message = {
            "entity_id": entity_id,
            "state": state,
            "attributes": dict(attributes or {}),
            "last_changed": stamp if changed or old is None else old["last_changed"],
            "last_updated": stamp,
            "context": dict(context or make_context()),
        }
        self.states[entity_id] = new
        return self._emit_state_changed(entity_id, old, new)

    def remove_state(self, entity_id: str) -> asyncio.Task[None]:
        """Remove an entity and send ``state_changed`` with no new state."""
        old = self.states.pop(entity_id, None)
        return self._emit_state_changed(entity_id, old, None)

    def fire_event(
        self,
        event_type: str,
        data: Mapping[str, Any],
        *,
        context: Mapping[str, Any] | None = None,
    ) -> asyncio.Task[None]:
        """Send any event to the subscribers of its type.

        ``context`` sets fields of the event context, for example
        ``{"user_id": ...}``. The default context has no user.
        """
        event = {
            "event_type": event_type,
            "data": dict(data),
            "origin": "LOCAL",
            "time_fired": now_iso(),
            "context": {**make_context(), **dict(context or {})},
        }
        return asyncio.create_task(self._broadcast(event))

    async def drop(self) -> None:
        """Close every client connection."""
        conns, self._conns = self._conns, []
        for conn in conns:
            with contextlib.suppress(Exception):
                await conn.ws.close()

    async def restart(self) -> None:
        """Close every connection and refuse new ones until ``up``."""
        self._down = True
        await self.drop()

    def up(self) -> None:
        """Accept connections again after ``restart``."""
        self._down = False

    def registry_updated(self, kind: str) -> asyncio.Task[None]:
        """Send ``<kind>_registry_updated``, as HA does after a change."""
        return self.fire_event(f"{kind}_registry_updated", {"action": "update"})

    def ha_commands(self) -> list[Message]:
        """The commands that act on HA or read HA data.

        This leaves out the commands that the registry loader, the state
        cache and the subscriptions send.
        """
        background = {"get_states", "get_services", "subscribe_events"}
        background |= {f"config/{k}_registry/list" for k in self.registry}
        return [c for c in self.commands if c.get("type") not in background]

    def commands_of(self, kind: str) -> list[Message]:
        """The recorded commands of one type."""
        return [c for c in self.commands if c.get("type") == kind]

    # Server side

    def _emit_state_changed(
        self, entity_id: str, old: Message | None, new: Message | None
    ) -> asyncio.Task[None]:
        context = (new or old or {}).get("context") or make_context()
        event = {
            "event_type": "state_changed",
            "data": {"entity_id": entity_id, "old_state": old, "new_state": new},
            "origin": "LOCAL",
            "time_fired": now_iso(),
            "context": context,
        }
        return asyncio.create_task(self._broadcast(event))

    async def _broadcast(self, event: Message) -> None:
        for conn in list(self._conns):
            for sub_id, event_type in list(conn.subs.items()):
                if event_type in (None, event["event_type"]):
                    with contextlib.suppress(ConnectionError, RuntimeError):
                        await conn.ws.send_json(
                            {"id": sub_id, "type": "event", "event": event}
                        )

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        if self._down:
            raise web.HTTPServiceUnavailable
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required", "ha_version": self.ha_version})
        auth = await ws.receive_json()
        if auth.get("type") != "auth" or auth.get("access_token") != self.token:
            await ws.send_json({"type": "auth_invalid", "message": "Invalid access"})
            await ws.close()
            return ws
        await ws.send_json({"type": "auth_ok", "ha_version": self.ha_version})
        self.connections_accepted += 1
        conn = _Conn(ws)
        self._conns.append(conn)
        try:
            async for frame in ws:
                if frame.type is WSMsgType.TEXT:
                    await self._command(conn, frame.json())
        finally:
            if conn in self._conns:
                self._conns.remove(conn)
        return ws

    async def _command(self, conn: _Conn, msg: Message) -> None:
        self.commands.append(msg)
        msg_id = msg.get("id")
        kind = msg.get("type")
        try:
            if kind in self.handlers:
                result = self.handlers[kind](self, msg)
                if asyncio.iscoroutine(result):
                    result = await result
            elif kind == "get_states":
                result = list(self.states.values())
            elif isinstance(kind, str) and kind in self._data_commands():
                result = self._data_commands()[kind](msg)
            elif kind == "subscribe_events":
                conn.subs[msg_id] = msg.get("event_type")  # type: ignore[index]
                result = None
            elif kind == "unsubscribe_events":
                if conn.subs.pop(msg.get("subscription"), "missing") == "missing":  # type: ignore[arg-type]
                    await self._error(
                        conn, msg_id, "not_found", "Subscription not found."
                    )
                    return
                result = None
            elif kind == "call_service":
                result = await self._call_service(msg)
            else:
                await self._error(conn, msg_id, "unknown_command", "Unknown command.")
                return
        except FakeCommandError as err:
            await self._error(conn, msg_id, err.code, err.message)
            return
        await conn.ws.send_json(
            {"id": msg_id, "type": "result", "success": True, "result": result}
        )

    def _data_commands(self) -> dict[str, Callable[[Message], Any]]:
        commands: dict[str, Callable[[Message], Any]] = {}
        for kind in self.registry:
            commands[f"config/{kind}_registry/list"] = partial(
                self._registry_list, kind
            )
        commands["config/entity_registry/get"] = self._entity_entry
        commands["get_services"] = lambda _m: self.services
        commands["auth/current_user"] = lambda _m: self.user
        commands["get_config"] = lambda _m: {
            "time_zone": self.time_zone,
            "version": self.ha_version,
        }
        commands["history/history_during_period"] = lambda _m: {
            e: [{"s": s["state"]}] for e, s in self.states.items()
        }
        commands["recorder/statistics_during_period"] = lambda _m: {
            e: [{"mean": 1.0}] for e in self.states
        }
        return commands

    def _registry_list(self, kind: str, _msg: Message) -> list[Message]:
        return list(self.registry[kind])

    def _entity_entry(self, msg: Message) -> Message:
        for entry in self.registry["entity"]:
            if entry.get("entity_id") == msg.get("entity_id"):
                return entry
        raise FakeCommandError("not_found", "Entity not found")

    async def _call_service(self, msg: Message) -> Message:
        context = make_context()
        service = msg.get("service")
        target = msg.get("target") or {}
        data = msg.get("service_data") or {}
        ids = target.get("entity_id", data.get("entity_id", []))
        ids = [ids] if isinstance(ids, str) else list(ids)
        tasks = []
        for entity_id in ids:
            old = self.states.get(entity_id)
            if old is None:
                continue
            if service == "turn_on":
                value = "on"
            elif service == "turn_off":
                value = "off"
            elif service == "toggle":
                value = "off" if old["state"] == "on" else "on"
            else:
                continue
            tasks.append(
                self.set_state(entity_id, value, old["attributes"], context=context)
            )
        # Real HA sends the state_changed events before the result.
        for task in tasks:
            await task
        result: Message = {"context": context}
        if msg.get("return_response"):
            result["response"] = {}
        return result

    async def _error(self, conn: _Conn, msg_id: Any, code: str, message: str) -> None:
        await conn.ws.send_json(
            {
                "id": msg_id,
                "type": "result",
                "success": False,
                "error": {"code": code, "message": message},
            }
        )


class FakeCommandError(Exception):
    """Raise it in a handler to send an error result."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
