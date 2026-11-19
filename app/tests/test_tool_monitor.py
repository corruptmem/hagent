"""The scope monitor: re-resolution after registry changes, and growth notices."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from hagent_app.agents import AgentReader
from hagent_app.agents.definition import AgentDefinition
from hagent_app.http import ChangeEvent, ChangeEvents
from hagent_app.tools.monitor import ScopeMonitor
from helpers.fake_ha import FakeCommandError, FakeHA
from helpers.tools import Harness, harness
from helpers.wait import wait_until

KITCHEN = {"tools__ha": {"level": "control", "scope": {"areas": ["kitchen"]}}}


@pytest.fixture
async def h(tmp_path: Path) -> AsyncIterator[Harness]:
    async with harness(tmp_path) as value:
        yield value


def _monitor(h: Harness, events: ChangeEvents | None = None) -> ScopeMonitor:
    return ScopeMonitor(
        h.db,
        AgentReader(h.db),
        h.client,
        events,
        known_entities=[s.entity_id for s in h.states],
    )


def _move_hall_to_kitchen(h: Harness) -> None:
    for entry in h.ha.registry["entity"]:
        if entry["entity_id"] == "light.hall":
            entry["area_id"] = "kitchen"


async def test_growth_sends_a_notification_and_an_event(h: Harness) -> None:
    events = ChangeEvents()
    seen: list[ChangeEvent] = []
    events.subscribe(seen.append)
    monitor = _monitor(h, events)
    h.registry.add_reload_listener(monitor.on_reload)
    agent = await h.agent(**KITCHEN)
    # The first resolution is the baseline. It sends nothing.
    assert await monitor.check(h.registry.current()) == []

    _move_hall_to_kitchen(h)
    h.reset()
    count = h.registry.reload_count
    await h.ha.registry_updated("entity")
    await wait_until(lambda: h.registry.reload_count > count)
    await wait_until(lambda: bool(h.ha.commands_of("call_service")))

    [command] = h.ha.commands_of("call_service")
    assert command["domain"] == "persistent_notification"
    assert command["service"] == "create"
    assert command["service_data"]["notification_id"] == f"hagent_scope_{agent.id}"
    assert "light.hall" in command["service_data"]["message"]
    [event] = seen
    assert (event.topic, event.action, event.id) == ("tools", "scope_grown", agent.id)
    assert event.data["added_control"] == ["light.hall"]


async def test_a_shrinking_scope_sends_nothing(h: Harness) -> None:
    monitor = _monitor(h)
    await h.agent(**KITCHEN)
    await monitor.check(h.registry.current())
    for entry in h.ha.registry["entity"]:
        if entry["entity_id"] == "switch.kettle":
            entry["area_id"] = None
    count = h.registry.reload_count
    await h.ha.registry_updated("entity")
    await wait_until(lambda: h.registry.reload_count > count)
    h.reset()
    assert await monitor.check(h.registry.current()) == []
    assert h.ha.commands_of("call_service") == []


async def test_a_new_definition_version_is_not_growth(h: Harness) -> None:
    monitor = _monitor(h)
    agent = await h.agent(**KITCHEN)
    await monitor.check(h.registry.current())
    data = agent.definition.to_data()
    data["tools"]["ha"]["scope"]["areas"] = ["kitchen", "lounge"]
    await h.agents.update(agent.id, AgentDefinition.from_json(json.dumps(data)))
    h.reset()
    assert await monitor.check(h.registry.current()) == []
    assert h.ha.commands_of("call_service") == []


async def test_growth_while_the_app_was_stopped_is_found(h: Harness) -> None:
    agent = await h.agent(**KITCHEN)
    await _monitor(h).check(h.registry.current())
    _move_hall_to_kitchen(h)
    count = h.registry.reload_count
    await h.ha.registry_updated("entity")
    await wait_until(lambda: h.registry.reload_count > count)
    # A new monitor reads the stored baseline from the database.
    growth = await _monitor(h).check(h.registry.current())
    assert [g.agent_id for g in growth] == [agent.id]
    assert growth[0].added_control == {"light.hall"}


async def test_all_and_admin_scopes_do_not_report_growth(h: Harness) -> None:
    monitor = _monitor(h)
    await h.agent(tools__ha={"level": "read", "scope": {"all": True}})
    await h.agent(tools__ha={"level": "admin"})
    await monitor.check(h.registry.current())
    h.ha.registry["entity"].append(
        {"entity_id": "light.new", "area_id": "kitchen", "labels": []}
    )
    count = h.registry.reload_count
    await h.ha.registry_updated("entity")
    await wait_until(lambda: h.registry.reload_count > count)
    assert await monitor.check(h.registry.current()) == []


async def test_domain_scope_reports_a_new_entity(h: Harness) -> None:
    monitor = _monitor(h)
    await h.agent(tools__ha={"level": "read", "scope": {"domains": ["lock"]}})
    await monitor.check(h.registry.current())
    h.ha.registry["entity"].append({"entity_id": "lock.back", "labels": []})
    count = h.registry.reload_count
    await h.ha.registry_updated("entity")
    await wait_until(lambda: h.registry.reload_count > count)
    growth = await monitor.check(h.registry.current())
    assert growth[0].added_read == {"lock.back"}


async def test_failed_notification_keeps_the_old_baseline(h: Harness) -> None:
    # Review-8 finding 5: a lost notification must be sent again.
    agent = await h.agent(**KITCHEN)
    monitor = _monitor(h)
    await monitor.check(h.registry.current())
    _move_hall_to_kitchen(h)
    count = h.registry.reload_count
    await h.ha.registry_updated("entity")
    await wait_until(lambda: h.registry.reload_count > count)

    def refuse(_ha: FakeHA, _msg: dict[str, Any]) -> None:
        raise FakeCommandError("timeout", "HA did not answer")

    h.ha.handlers["call_service"] = refuse
    growth = await monitor.check(h.registry.current())
    assert [g.agent_id for g in growth] == [agent.id]

    # The next check finds the same growth and sends the notification.
    del h.ha.handlers["call_service"]
    h.reset()
    growth = await monitor.check(h.registry.current())
    assert growth[0].added_control == {"light.hall"}
    [command] = h.ha.commands_of("call_service")
    assert command["service_data"]["notification_id"] == f"hagent_scope_{agent.id}"

    # HA accepted it, so the new baseline is stored.
    h.reset()
    assert await monitor.check(h.registry.current()) == []
    assert h.ha.commands_of("call_service") == []
