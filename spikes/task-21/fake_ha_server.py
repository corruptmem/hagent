"""Run FakeHA as a server for the app image smoke test.

The smoke test starts this script in a container from the app image. The app
container shares its network namespace, so the app reaches the fake at
``ws://127.0.0.1:8123/api/websocket``.

Signals:

- SIGUSR1: simulate an HA restart. The fake closes every connection,
  refuses new ones for 5 seconds, then accepts them again.
- SIGTERM or SIGINT: stop.

``PYTHONPATH`` must hold the parent folder of ``app/tests/helpers``.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from aiohttp.test_utils import TestServer
from helpers.fake_ha import FakeHA

PORT = 8123
RESTART_SECONDS = 5.0

_LOG = logging.getLogger("fake_ha")


async def _restart(fake: FakeHA) -> None:
    _LOG.info("simulating an HA restart")
    await fake.restart()
    await asyncio.sleep(RESTART_SECONDS)
    fake.up()
    _LOG.info("HA is up again")


async def main() -> None:
    """Serve FakeHA on a fixed port until a stop signal arrives."""
    fake = FakeHA()
    # FakeHA picks a free port. The smoke test needs a fixed one.
    fake._server = TestServer(fake._server.app, host="127.0.0.1", port=PORT)
    await fake.set_state("light.smoke_test", "off", {"friendly_name": "Smoke test"})
    await fake.start()
    _LOG.info("FakeHA listens on %s", fake.url)

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    tasks: set[asyncio.Task[None]] = set()

    def restart() -> None:
        task = asyncio.create_task(_restart(fake))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    loop.add_signal_handler(signal.SIGUSR1, restart)
    loop.add_signal_handler(signal.SIGTERM, stop.set)
    loop.add_signal_handler(signal.SIGINT, stop.set)
    await stop.wait()
    await fake.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    asyncio.run(main())
