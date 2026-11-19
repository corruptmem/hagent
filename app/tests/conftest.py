"""Shared fixtures."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from helpers.fake_ha import FakeHA
from helpers.providers_env import ProvidersEnv


@pytest.fixture
async def fake_ha() -> AsyncIterator[FakeHA]:
    """A running fake HA WebSocket server."""
    async with FakeHA() as ha:
        yield ha


@pytest.fixture
async def providers_env(
    tmp_path: Path,
) -> AsyncIterator[Callable[..., Awaitable[ProvidersEnv]]]:
    """Start a ``ProvidersComponent`` on a temporary database.

    Call the fixture value with optional ``providers`` options and a clock.
    """
    envs: list[ProvidersEnv] = []

    async def start(
        clock: Callable[[], datetime] | None = None, **options: Any
    ) -> ProvidersEnv:
        env = await ProvidersEnv.start(tmp_path / f"env{len(envs)}", clock, options)
        envs.append(env)
        return env

    yield start
    for env in envs:
        await env.close()
