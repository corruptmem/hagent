"""Fixtures for the hagent integration tests.

``FakeApp`` is a fake hagent app. It serves the link WebSocket on
127.0.0.1, checks the secret, and sends the example messages of
``docs/link-messages.json``. No test needs a live HA instance or the real
app.
"""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from aiohttp import WSMsgType, web
from custom_components.hagent.const import CONF_SECRET, DOMAIN, LINK_PATH
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

FIXTURE = Path(__file__).resolve().parents[2] / "docs" / "link-messages.json"
SECRET = "test-secret-0123456789abcdefghijklmnopqrstuvwxyz"

type Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

# The fields of each command in docs/link.md. The fake app records each
# command with another field, so that a misnamed field fails the test.
COMMAND_FIELDS = {
    "invoke": {
        "type",
        "id",
        "agent_id",
        "source",
        "text",
        "data",
        "conversation_id",
        "result_schema",
        "attachments",
        "caller",
        "wait",
        "timeout",
        "on_timeout",
    },
    "cancel": {"type", "id", "invocation_id"},
    "set_enabled": {"type", "id", "agent_id", "enabled"},
    "ping": {"type", "id"},
}


def messages() -> dict[str, Any]:
    """A fresh copy of the shared example messages."""
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return copy.deepcopy(data)


def agent(**changes: Any) -> dict[str, Any]:
    """An agent snapshot from the shared fixture, with changes."""
    value: dict[str, Any] = messages()["hello"]["agents"][0]
    value.update(changes)
    return value


class FakeApp:
    """A fake hagent app with a scripted link."""

    def __init__(self) -> None:
        self.secret = SECRET
        self.agents: list[dict[str, Any]] = [agent()]
        self.installation_id = "installation-1"
        self.commands: list[dict[str, Any]] = []
        self.bad_commands: list[dict[str, Any]] = []
        self.handlers: dict[str, Handler] = {}
        self.sockets: list[web.WebSocketResponse] = []
        self.refused = 0
        self.connects = 0
        self._runner: web.AppRunner | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self.port = 0

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get(LINK_PATH, self._ws)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", self.port)
        await site.start()
        self.port = self._runner.addresses[0][1]

    async def stop(self) -> None:
        await self.drop()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def drop(self) -> None:
        """Close every link connection."""
        for ws in list(self.sockets):
            await ws.close()

    async def push(self, message: dict[str, Any]) -> None:
        """Send a message to every connected client."""
        for ws in list(self.sockets):
            await ws.send_json(message)

    async def _ws(self, request: web.Request) -> web.StreamResponse:
        if request.headers.get("Authorization") != f"Bearer {self.secret}":
            self.refused += 1
            return web.json_response({"error": {"code": "unauthorized"}}, status=401)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.connects += 1
        self.sockets.append(ws)
        hello = messages()["hello"]
        hello["installation_id"] = self.installation_id
        hello["agents"] = copy.deepcopy(self.agents)
        await ws.send_json(hello)
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                command = json.loads(msg.data)
                self.commands.append(command)
                allowed = COMMAND_FIELDS.get(command.get("type"), set())
                if not set(command) <= allowed:
                    self.bad_commands.append(command)
                task = asyncio.get_running_loop().create_task(self._answer(ws, command))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        finally:
            self.sockets.remove(ws)
        return ws

    async def _answer(self, ws: web.WebSocketResponse, command: dict[str, Any]) -> None:
        handler = self.handlers.get(command["type"])
        if handler is None:
            reply = messages()["invoke_response"]
        else:
            reply = await handler(command)
        reply["id"] = command["id"]
        if not ws.closed:
            await ws.send_json(reply)


@pytest.fixture(autouse=True)
async def auto_enable_custom_integrations(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    """Let HA load the custom integration.

    The conversation component needs the exposed-entities store of the core
    integration, so the fixture sets up the core integration first.
    """
    assert await async_setup_component(hass, "homeassistant", {})


@pytest.fixture
async def fake_app(socket_enabled: None) -> AsyncIterator[FakeApp]:
    """A started fake app on 127.0.0.1. HA tests block sockets by default."""
    app = FakeApp()
    await app.start()
    try:
        yield app
    finally:
        await app.stop()
    assert app.bad_commands == [], "commands with fields that docs/link.md lacks"


def make_entry(app: FakeApp, **data: Any) -> MockConfigEntry:
    values = {
        "host": "127.0.0.1",
        "port": app.port,
        CONF_SECRET: app.secret,
        **data,
    }
    return MockConfigEntry(
        domain=DOMAIN, data=values, unique_id=app.installation_id, title="hagent"
    )


async def setup_entry(
    hass: HomeAssistant, app: FakeApp, **data: Any
) -> MockConfigEntry:
    entry = make_entry(app, **data)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def wait_for(
    hass: HomeAssistant,
    check: Callable[[], bool],
    timeout: float = 5.0,  # noqa: ASYNC109 - a test deadline
) -> None:
    """Wait until ``check`` is true."""
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(0.01)
            await hass.async_block_till_done()
