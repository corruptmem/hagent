"""Wait helpers for async tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable


async def wait_until(check: Callable[[], bool], limit: float = 2.0) -> None:
    """Poll ``check`` until it is true. Fail after ``limit`` seconds."""
    async with asyncio.timeout(limit):
        while not check():  # noqa: ASYNC110 - polls test state, not one event
            await asyncio.sleep(0.005)
