"""Live probe for task-2: link a call_service call to its state_changed events.

The probe creates a temporary input_boolean helper, changes it through the
WebSocket API, records message order and context IDs, then deletes the helper.
It touches no other entity. It prints only a redacted JSON report.

Run:
    set -a; . <spike env file>; set +a
    uv run --with websockets python spikes/task-2/probe.py

Environment: HASS_URL (for example http://host:8123) and HASS_TOKEN.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from typing import Any

import websockets

PLACEHOLDER = "input_boolean.<test>"


class Client:
    """A small WebSocket client that numbers every inbound message."""

    def __init__(self, ws: Any) -> None:
        self.ws = ws
        self.next_id = 1
        self.seq = 0
        self.log: list[dict[str, Any]] = []

    async def send(self, msg: dict[str, Any]) -> int:
        msg_id = self.next_id
        self.next_id += 1
        await self.ws.send(json.dumps({"id": msg_id, **msg}))
        return msg_id

    async def recv(self, wait: float = 5.0) -> dict[str, Any]:
        raw = await asyncio.wait_for(self.ws.recv(), wait)
        self.seq += 1
        msg: dict[str, Any] = json.loads(raw)
        msg["_seq"] = self.seq
        msg["_t"] = time.monotonic()
        self.log.append(msg)
        return msg

    async def result(self, msg_id: int) -> dict[str, Any]:
        while True:
            msg = await self.recv()
            if msg.get("type") == "result" and msg.get("id") == msg_id:
                return msg

    async def call(self, msg: dict[str, Any]) -> dict[str, Any]:
        return await self.result(await self.send(msg))

    async def drain(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while (left := end - time.monotonic()) > 0:
            try:
                await self.recv(wait=left)
            except TimeoutError:
                return


async def connect(url: str, token: str) -> tuple[Any, Client, str]:
    ws = await websockets.connect(url, max_size=None)
    hello = json.loads(await ws.recv())
    await ws.send(json.dumps({"type": "auth", "access_token": token}))
    auth = json.loads(await ws.recv())
    if auth.get("type") != "auth_ok":
        msg = "authentication failed"
        raise RuntimeError(msg)
    return ws, Client(ws), str(hello.get("ha_version"))


def ctx(c: dict[str, Any] | None) -> dict[str, Any] | None:
    """Redact the user ID but keep its presence."""
    if c is None:
        return None
    return {
        "id": c.get("id"),
        "parent_id": c.get("parent_id"),
        "user_id": "<set>" if c.get("user_id") else None,
    }


def events_for(client: Client, sub_id: int, entity: str, since: int) -> list[Any]:
    out = []
    for m in client.log:
        if m["_seq"] <= since or m.get("type") != "event" or m.get("id") != sub_id:
            continue
        data = m["event"]["data"]
        if data.get("entity_id") != entity:
            continue
        new = data.get("new_state") or {}
        out.append(
            {
                "seq": m["_seq"],
                "event_context": ctx(m["event"].get("context")),
                "new_state": new.get("state"),
                "new_state_context": ctx(new.get("context")),
            }
        )
    return out


async def step(
    client: Client, sub: int, entity: str, name: str, msg: dict[str, Any]
) -> dict[str, Any]:
    start = client.seq
    res = await client.call(msg)
    await client.drain(1.0)
    rc = (res.get("result") or {}).get("context")
    evs = events_for(client, sub, entity, start)
    return {
        "step": name,
        "success": res.get("success"),
        "error_code": (res.get("error") or {}).get("code"),
        "result_seq": res["_seq"],
        "result_context": ctx(rc),
        "events": evs,
        "event_before_result": [e["seq"] < res["_seq"] for e in evs],
        "event_context_equals_result": [
            rc is not None and e["event_context"]["id"] == rc["id"] for e in evs
        ],
        "state_context_equals_result": [
            rc is not None and e["new_state_context"]["id"] == rc["id"] for e in evs
        ],
    }


def toggle(entity: str, service: str, **extra: Any) -> dict[str, Any]:
    return {
        "type": "call_service",
        "domain": "input_boolean",
        "service": service,
        "target": {"entity_id": entity},
        **extra,
    }


async def main() -> None:
    base = os.environ["HASS_URL"].rstrip("/")
    url = base.replace("http", "ws", 1) + "/api/websocket"
    token = os.environ["HASS_TOKEN"]

    ws, client, version = await connect(url, token)
    ws2, other, _ = await connect(url, token)
    report: dict[str, Any] = {"ha_version": version, "steps": []}
    helper_id: str | None = None
    try:
        sub = await client.send(
            {"type": "subscribe_events", "event_type": "state_changed"}
        )
        await client.result(sub)

        name = f"hagent spike {secrets.token_hex(4)}"
        created = await client.call({"type": "input_boolean/create", "name": name})
        helper_id = created["result"]["id"]
        entity = f"input_boolean.{helper_id}"
        await client.drain(1.0)
        report["created"] = bool(events_for(client, sub, entity, 0))

        steps = report["steps"]
        steps.append(
            await step(client, sub, entity, "turn_on", toggle(entity, "turn_on"))
        )
        steps.append(
            await step(client, sub, entity, "turn_on_noop", toggle(entity, "turn_on"))
        )
        steps.append(
            await step(
                client,
                sub,
                entity,
                "turn_off_with_client_context",
                toggle(
                    entity, "turn_off", context={"id": "01HAGENTSPIKE0000000000000"}
                ),
            )
        )
        steps.append(
            await step(client, sub, entity, "toggle", toggle(entity, "toggle"))
        )

        # An external change: another connection toggles the helper.
        start = client.seq
        ext = await other.call(toggle(entity, "toggle"))
        await client.drain(1.0)
        ext_ctx = (ext.get("result") or {}).get("context") or {}
        own_ids = {s["result_context"]["id"] for s in steps if s["result_context"]}
        evs = events_for(client, sub, entity, start)
        steps.append(
            {
                "step": "external_toggle_other_connection",
                "events": evs,
                "matches_own_call": [e["event_context"]["id"] in own_ids for e in evs],
                "matches_other_result": [
                    e["event_context"]["id"] == ext_ctx.get("id") for e in evs
                ],
            }
        )
    finally:
        if helper_id is not None:
            res = await client.call(
                {"type": "input_boolean/delete", "input_boolean_id": helper_id}
            )
            listed = await client.call({"type": "input_boolean/list"})
            ids = {item.get("id") for item in listed.get("result") or []}
            report["deleted"] = bool(res.get("success")) and helper_id not in ids
        await ws.close()
        await ws2.close()

    text = json.dumps(report, indent=2)
    if helper_id is not None:
        text = text.replace(f"input_boolean.{helper_id}", PLACEHOLDER)
        text = text.replace(helper_id, "<test>")
    print(text)


if __name__ == "__main__":
    asyncio.run(main())
