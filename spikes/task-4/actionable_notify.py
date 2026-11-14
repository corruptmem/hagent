# /// script
# requires-python = ">=3.14"
# dependencies = ["aiohttp>=3.12"]
# ///
"""Spike probe for task-4: an actionable HA notification for an approval.

The probe does these steps:

1. It connects to the HA WebSocket API and subscribes to the
   `mobile_app_notification_action` event.
2. It sends one notification with an accept action and a reject action to one
   mobile_app notify target. Each action ID contains a random request ID.
3. It waits for an event whose action ID contains that request ID.
4. If no event arrives before the time limit, it clears the notification on
   the phone with the `clear_notification` message and the same tag.

The probe makes no change to HA configuration.

Environment variables (never commit values):

- HASS_URL: base URL of HA, for example `http://<host>:8123`.
- HASS_TOKEN: a long-lived access token.
- HAGENT_NOTIFY_TARGET: the notify service, for example
  `mobile_app_<device>` (without the `notify.` domain).
- HAGENT_APPROVER_PERSON: optional. A person entity, for example
  `person.<name>`. The probe then checks that the HA user of the event
  context is the user of that person.

Run: `uv run --script spikes/task-4/actionable_notify.py [timeout_seconds]`
"""

from __future__ import annotations

import asyncio
import itertools
import os
import secrets
import sys
import time
from typing import Any

import aiohttp

EVENT_TYPE = "mobile_app_notification_action"
# Older iOS app versions fire these event types. The probe logs them too.
LEGACY_EVENT_TYPES = ("ios.notification_action_fired", "ios.action_fired")
DEFAULT_TIMEOUT_S = 600
ACCEPT = "ACCEPT"
REJECT = "REJECT"


class HaSocket:
    """A minimal client for the HA WebSocket API."""

    def __init__(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self._ws = ws
        self._ids = itertools.count(1)
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._results: dict[int, asyncio.Future[dict[str, Any]]] = {}

    async def auth(self, token: str) -> None:
        msg = await self._ws.receive_json()
        if msg["type"] != "auth_required":
            raise RuntimeError(msg["type"])
        await self._ws.send_json({"type": "auth", "access_token": token})
        msg = await self._ws.receive_json()
        if msg["type"] != "auth_ok":
            raise RuntimeError(msg["type"])

    async def reader(self) -> None:
        async for raw in self._ws:
            msg = raw.json()
            if msg["type"] == "event":
                await self.events.put(msg["event"])
            elif msg["type"] == "result":
                fut = self._results.pop(msg["id"], None)
                if fut is not None:
                    fut.set_result(msg)

    async def call(self, payload: dict[str, Any]) -> dict[str, Any]:
        msg_id = next(self._ids)
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._results[msg_id] = fut
        await self._ws.send_json({"id": msg_id, **payload})
        result = await fut
        if not result.get("success"):
            raise RuntimeError(result.get("error"))
        return result


async def person_user_id(sock: HaSocket, person: str) -> str | None:
    states = (await sock.call({"type": "get_states"}))["result"]
    for state in states:
        if state["entity_id"] == person:
            user_id: str | None = state["attributes"].get("user_id")
            return user_id
    return None


def log(text: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {text}", flush=True)


async def run(timeout_s: float) -> int:
    url = os.environ["HASS_URL"].rstrip("/") + "/api/websocket"
    token = os.environ["HASS_TOKEN"]
    target = os.environ["HAGENT_NOTIFY_TARGET"]
    person = os.environ.get("HAGENT_APPROVER_PERSON")

    request_id = secrets.token_hex(4)
    tag = f"hagent-approval-{request_id}"
    accept_id = f"HAGENT_{ACCEPT}_{request_id}"
    reject_id = f"HAGENT_{REJECT}_{request_id}"

    async with (
        aiohttp.ClientSession() as session,
        session.ws_connect(url) as ws,
    ):
        sock = HaSocket(ws)
        await sock.auth(token)
        reader = asyncio.create_task(sock.reader())
        expected_user = await person_user_id(sock, person) if person else None

        for event_type in (EVENT_TYPE, *LEGACY_EVENT_TYPES):
            await sock.call({"type": "subscribe_events", "event_type": event_type})
        await sock.call(
            {
                "type": "call_service",
                "domain": "notify",
                "service": target,
                "service_data": {
                    "title": "hagent approval test",
                    "message": (
                        "Spike test. Tap and hold, then choose Accept or Reject. "
                        f"Request {request_id}."
                    ),
                    "data": {
                        "tag": tag,
                        "actions": [
                            {"action": accept_id, "title": "Accept"},
                            {
                                "action": reject_id,
                                "title": "Reject",
                                "destructive": True,
                            },
                        ],
                    },
                },
            }
        )
        sent_at = time.monotonic()
        log(f"SENT request={request_id} timeout={timeout_s:.0f}s")

        outcome = await wait_for_answer(sock, request_id, timeout_s)
        if outcome is None:
            log("TIMEOUT: no answer. The request counts as rejected.")
            await sock.call(
                {
                    "type": "call_service",
                    "domain": "notify",
                    "service": target,
                    "service_data": {
                        "message": "clear_notification",
                        "data": {"tag": tag},
                    },
                }
            )
            log("CLEARED the notification on the device (tag).")
            reader.cancel()
            return 2

        decision, event = outcome
        latency = time.monotonic() - sent_at
        ctx_user = event.get("context", {}).get("user_id")
        log(f"ANSWER decision={decision} after={latency:.1f}s")
        log(f"event data keys={sorted(event.get('data', {}))}")
        log(f"event origin={event.get('origin')} context.user_id set={bool(ctx_user)}")
        if expected_user is not None:
            log(f"context user matches approver person={ctx_user == expected_user}")
        reader.cancel()
        return 0


async def wait_for_answer(
    sock: HaSocket, request_id: str, timeout_s: float
) -> tuple[str, dict[str, Any]] | None:
    deadline = time.monotonic() + timeout_s
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            event = await asyncio.wait_for(sock.events.get(), remaining)
        except TimeoutError:
            return None
        action = str(event.get("data", {}).get("action", ""))
        if event.get("event_type") != EVENT_TYPE or not action.endswith(
            f"_{request_id}"
        ):
            log(f"ignored event type={event.get('event_type')} action={action!r}")
            continue
        decision = ACCEPT if action.startswith(f"HAGENT_{ACCEPT}_") else REJECT
        return decision, event
    return None


def main() -> None:
    timeout_s = float(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_TIMEOUT_S
    sys.exit(asyncio.run(run(timeout_s)))


if __name__ == "__main__":
    main()
