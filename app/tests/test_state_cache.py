import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from hagent_app.ha import HAClient, StateCache
from helpers.fake_ha import FakeHA, make_context
from helpers.wait import wait_until

FAST = {"backoff_initial": 0.01, "backoff_max": 0.05, "timeout": 2.0}


@pytest.fixture
async def cache(fake_ha: FakeHA) -> AsyncIterator[StateCache]:
    fake_ha.set_state("light.one", "off", {"brightness": 0})
    fake_ha.set_state("sensor.two", "21.5")
    client = HAClient(fake_ha.url, fake_ha.token, **FAST)
    states = StateCache(client)
    await states.start()
    await client.start()
    await asyncio.wait_for(states.wait_ready(), 2)
    yield states
    await states.stop()
    await client.stop()


async def test_initial_load(cache: StateCache) -> None:
    assert len(cache) == 2
    light = cache.get("light.one")
    assert light is not None
    assert light.state == "off"
    assert light.attributes["brightness"] == 0
    assert "sensor.two" in cache
    assert cache.get("sensor.absent") is None
    assert not cache.stale


async def test_updates_adds_and_removes(fake_ha: FakeHA, cache: StateCache) -> None:
    await fake_ha.set_state("light.one", "on", {"brightness": 200})
    await fake_ha.set_state("switch.new", "on")
    await fake_ha.remove_state("sensor.two")
    await wait_until(lambda: "sensor.two" not in cache and "switch.new" in cache)
    light = cache.get("light.one")
    assert light is not None
    assert light.state == "on"
    assert light.attributes["brightness"] == 200


async def test_service_call_updates_cache(fake_ha: FakeHA, cache: StateCache) -> None:
    ha = cache._client
    await ha.call_service("light", "turn_on", target={"entity_id": "light.one"})
    state = cache.get("light.one")
    # The event arrives before the result, so the cache is current already.
    assert state is not None
    assert state.state == "on"


async def test_keeps_data_while_ha_restarts_then_reloads(
    fake_ha: FakeHA, cache: StateCache
) -> None:
    await fake_ha.restart()
    await wait_until(lambda: cache.stale)
    light = cache.get("light.one")
    assert light is not None  # Still readable during the restart.
    assert light.state == "off"
    # HA changes while the app has no connection.
    fake_ha.states["light.one"]["state"] = "on"
    fake_ha.states["light.one"]["last_updated"] = datetime.now(UTC).isoformat()
    del fake_ha.states["sensor.two"]
    fake_ha.up()
    await wait_until(lambda: not cache.stale)
    light = cache.get("light.one")
    assert light is not None
    assert light.state == "on"
    assert "sensor.two" not in cache
    assert len(fake_ha.commands_of("get_states")) == 2


def _event(
    entity_id: str, new: dict[str, Any] | None, fired: datetime
) -> dict[str, Any]:
    return {
        "event_type": "state_changed",
        "time_fired": fired.isoformat(),
        "data": {"entity_id": entity_id, "old_state": None, "new_state": new},
        "context": make_context(),
    }


def _state(entity_id: str, value: str, at: datetime) -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "state": value,
        "attributes": {},
        "last_changed": at.isoformat(),
        "last_updated": at.isoformat(),
        "context": make_context(),
    }


async def test_events_held_during_load_merge_by_time(fake_ha: FakeHA) -> None:
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    old, snap, new = t0, t0 + timedelta(seconds=1), t0 + timedelta(seconds=2)
    fake_ha.states = {
        "a.x": _state("a.x", "snapshot", snap),
        "b.x": _state("b.x", "snapshot", snap),
        "c.x": _state("c.x", "snapshot", snap),
    }
    client = HAClient(fake_ha.url, fake_ha.token, **FAST)
    cache = StateCache(client)
    await cache.start()

    async def get_states_with_race(ha: FakeHA, _msg: dict[str, Any]) -> list[Any]:
        snapshot = list(ha.states.values())
        # These events reach the client after the subscribe and before the
        # get_states result, so the cache holds them during the load.
        await ha._broadcast(_event("a.x", _state("a.x", "older", old), old))
        await ha._broadcast(_event("b.x", _state("b.x", "newer", new), new))
        await ha._broadcast(_event("c.x", None, new))
        await ha._broadcast(_event("d.x", _state("d.x", "created", new), new))
        return snapshot

    fake_ha.handlers["get_states"] = get_states_with_race
    await client.start()
    try:
        await asyncio.wait_for(cache.wait_ready(), 2)
        a, b, d = cache.get("a.x"), cache.get("b.x"), cache.get("d.x")
        assert a is not None
        assert a.state == "snapshot"  # The older event is skipped.
        assert b is not None
        assert b.state == "newer"
        assert "c.x" not in cache
        assert d is not None
        assert d.state == "created"
    finally:
        await cache.stop()
        await client.stop()
