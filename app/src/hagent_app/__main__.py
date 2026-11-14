"""Start the hagent app: ``python -m hagent_app``.

The app stops cleanly on SIGTERM (s6-overlay sends it when the container
stops) and on SIGINT.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from hagent_app.app import App
from hagent_app.config import ConfigError, load_config

_LOG = logging.getLogger("hagent_app")


async def _main() -> None:
    config = load_config()
    logging.getLogger().setLevel(config.log_level)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await App(config).run(stop)


def main() -> int:
    """Run the app until a stop signal. Return the exit status."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(_main())
    except ConfigError as err:
        _LOG.error("configuration error: %s", err)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
