import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from hagent_app.ha import HAClient, HACommandError, HAConnectionError
from hagent_app.own_writes import OwnWriteTracker
from helpers.fake_ha import FakeCommandError, FakeHA
from helpers.wait import wait_until

FAST = {"backoff_initial": 0.01, "backoff_max": 0.05, "timeout": 2.0}


@pytest.fixture
async def client(fake_ha: FakeHA) -> AsyncIterator[HAClient]:
    ha = HAClient(fake_ha.url, fake_ha.token, **FAST)
    await ha.start()
    await asyncio.wait_for(ha.wait_connected(), 2)
    yield ha
    await ha.stop()


async def test_connects_and_authenticates(fake_ha: FakeHA, client: HAClient) -> None:
    assert client.connected
    assert client.ha_version == fake_ha.ha_version
    assert fake_ha.connections_accepted == 1


async def test_bad_token_never_connects(fake_ha: FakeHA) -> None:
    ha = HAClient(fake_ha.url, "wrong", **FAST)
    await ha.start()
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(ha.wait_connected(), 0.2)
        assert fake_ha.connections_accepted == 0
    finally:
        await ha.stop()


async def test_command_without_connection_fails(fake_ha: FakeHA) -> None:
    ha = HAClient(fake_ha.url, fake_ha.token, **FAST)
    with pytest.raises(HAConnectionError):
        await ha.get_states()


async def test_call_service_returns_context_and_events_come_first(
    fake_ha: FakeHA, client: HAClient
) -> None:
    await fake_ha.set_state("light.test", "off")
    seen: list[str] = []
    events: list[dict[str, Any]] = []

    def on_event(event: dict[str, Any]) -> None:
        seen.append("event")
        events.append(event)

    await client.subscribe_events(on_event, "state_changed")
    result = await client.call_service(
        "light", "turn_on", target={"entity_id": "light.test"}
    )
    seen.append("result")
    assert seen == ["event", "result"]
    assert result.context_id is not None
    assert events[0]["context"]["id"] == result.context_id
    sent = fake_ha.commands_of("call_service")[-1]
    assert sent["target"] == {"entity_id": "light.test"}

    # The result context feeds the own-write tracker from task-2.
    tracker: OwnWriteTracker[dict[str, Any]] = OwnWriteTracker()
    token = tracker.call_started("agent-1")
    held = tracker.event(events[0]["context"]["id"], events[0])
    assert held == []
    (classified,) = tracker.call_finished(token, result.context_id)
    assert classified.origin == "agent"
    assert classified.agent_id == "agent-1"


async def test_command_error(fake_ha: FakeHA, client: HAClient) -> None:
    def fail(_ha: FakeHA, _msg: dict[str, Any]) -> None:
        raise FakeCommandError("invalid_format", "bad")

    fake_ha.handlers["render_template"] = fail
    with pytest.raises(HACommandError) as info:
        await client.command({"type": "render_template", "template": "x"})
    assert info.value.code == "invalid_format"
    with pytest.raises(HACommandError, match="unknown_command"):
        await client.command({"type": "nothing"})


async def test_pending_command_fails_on_disconnect(
    fake_ha: FakeHA, client: HAClient
) -> None:
    gate = asyncio.Event()

    async def slow(_ha: FakeHA, _msg: dict[str, Any]) -> None:
        await gate.wait()

    fake_ha.handlers["slow"] = slow
    task = asyncio.create_task(client.command({"type": "slow"}))
    await wait_until(lambda: bool(fake_ha.commands_of("slow")))
    await fake_ha.restart()
    with pytest.raises(HAConnectionError):
        await task
    gate.set()
    fake_ha.up()


async def test_reconnects_and_resubscribes(fake_ha: FakeHA, client: HAClient) -> None:
    events: list[str] = []
    disconnects = 0

    async def on_disconnect() -> None:
        nonlocal disconnects
        disconnects += 1

    client.add_disconnect_listener(on_disconnect)
    sub = await client.subscribe_events(
        lambda e: events.append(e["data"]["entity_id"]), "state_changed"
    )
    await fake_ha.restart()
    await wait_until(lambda: not client.connected)
    assert disconnects == 1
    with pytest.raises(HAConnectionError):
        await client.get_states()
    await asyncio.sleep(0.1)  # Several refused attempts while HA is down.
    fake_ha.up()
    await asyncio.wait_for(client.wait_connected(), 2)
    assert client.connect_count == 2
    assert len(fake_ha.commands_of("subscribe_events")) == 2
    await fake_ha.set_state("sensor.after", "1")
    await wait_until(lambda: events == ["sensor.after"])

    await sub.unsubscribe()
    assert fake_ha.commands_of("unsubscribe_events")
    await fake_ha.set_state("sensor.after", "2")
    await asyncio.sleep(0.05)
    assert events == ["sensor.after"]


async def test_subscription_before_connect(fake_ha: FakeHA) -> None:
    ha = HAClient(fake_ha.url, fake_ha.token, **FAST)
    events: list[str] = []
    await ha.subscribe_events(lambda e: events.append(e["event_type"]), "custom")
    await ha.start()
    try:
        await asyncio.wait_for(ha.wait_connected(), 2)
        await fake_ha.fire_event("custom", {})
        await fake_ha.fire_event("other", {})
        await wait_until(lambda: events == ["custom"])
    finally:
        await ha.stop()


async def test_handler_error_does_not_stop_reader(
    fake_ha: FakeHA, client: HAClient
) -> None:
    good: list[str] = []

    def bad(_event: dict[str, Any]) -> None:
        raise RuntimeError

    await client.subscribe_events(bad, "x")
    await client.subscribe_events(lambda e: good.append(e["event_type"]), "x")
    await fake_ha.fire_event("x", {})
    await wait_until(lambda: good == ["x"])
    assert client.connected


async def test_no_op_service_call_sends_no_event(
    fake_ha: FakeHA, client: HAClient
) -> None:
    # Task-2 saw this on real HA: a turn_on of an entity that is on already
    # returns a context but causes no state_changed event.
    await fake_ha.set_state("light.test", "on")
    events: list[dict[str, Any]] = []
    await client.subscribe_events(events.append, "state_changed")
    result = await client.call_service(
        "light", "turn_on", target={"entity_id": "light.test"}
    )
    await asyncio.sleep(0.05)
    assert result.context_id is not None
    assert events == []
