"""Check the HTTP surfaces of a running app container.

The smoke test runs this script in the FakeHA container. ``PROBE_HOST`` is
the host name of the app container. The options of the smoke test allow
the smoke network in the Ingress guard.

The script prints one line for each check and never prints the link secret.
It exits with status 1 if a check fails.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import aiohttp

HOST = os.environ.get("PROBE_HOST", "127.0.0.1")
BASE = f"http://{HOST}:8099/"
LINK = f"ws://{HOST}:8099/api/link/ws"
OK = 200
UNAUTHORIZED = 401


async def main() -> int:
    """Run the checks and return the exit status."""
    failures = 0

    def report(name: str, passed: bool, detail: str = "") -> None:
        nonlocal failures
        failures += 0 if passed else 1
        print(f"{'PASS' if passed else 'FAIL'} {name} {detail}".rstrip())

    async with aiohttp.ClientSession() as session:
        async with session.get(BASE) as resp:
            body = await resp.text()
            report(
                "ui index",
                resp.status == OK and "<html" in body.lower(),
                f"status={resp.status} type={resp.content_type}",
            )

        async with session.get(BASE + "api/agents") as resp:
            data = await resp.json()
            report(
                "api/agents",
                resp.status == OK and isinstance(data.get("agents"), list),
                f"status={resp.status} agents={len(data.get('agents', []))}",
            )

        async with session.get(BASE + "api/settings") as resp:
            report("api/settings", resp.status == OK, f"status={resp.status}")

        async with session.get(BASE + "api/integration") as resp:
            integration = await resp.json()
            secret = integration.get("secret", "")
            report(
                "api/integration",
                resp.status == OK and bool(secret),
                f"status={resp.status} port={integration.get('port')}",
            )

        async with session.get(LINK) as resp:
            report(
                "link without secret",
                resp.status == UNAUTHORIZED,
                f"status={resp.status}",
            )

        headers = {"Authorization": f"Bearer {secret}"}
        async with session.ws_connect(LINK, headers=headers) as ws:
            hello = json.loads((await ws.receive(timeout=10)).data)
            report(
                "link hello",
                hello.get("type") == "hello" and hello.get("protocol") == 1,
                f"installation_id={hello.get('installation_id')}",
            )

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
