"""Shared fixtures."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from helpers.fake_ha import FakeHA


@pytest.fixture
async def fake_ha() -> AsyncIterator[FakeHA]:
    """A running fake HA WebSocket server."""
    async with FakeHA() as ha:
        yield ha
