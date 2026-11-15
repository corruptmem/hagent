"""Tests of the hagent integration against a fake app (task-17).

The tests use a real HA Core instance in memory
(pytest-homeassistant-custom-component) and ``FakeApp``, a fake link
server. No test needs a live HA instance or the real app.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest
from custom_components.hagent import const
from custom_components.hagent.ai_task import (
    EVALUATE,
    HagentAITaskEntity,
    structure_schema,
)
from custom_components.hagent.const import DOMAIN, EVENT_RESULT
from custom_components.hagent.invoke import (
    HagentInvocationError,
    read_attachments,
)
from homeassistant.auth.models import User
from homeassistant.components import ai_task, conversation
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import Context, Event, HomeAssistant
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceValidationError,
    Unauthorized,
)
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .conftest import FakeApp, agent, messages, setup_entry, wait_for

AGENT = "agent-1"


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(const, "BACKOFF_INITIAL", 0.05)
    monkeypatch.setattr(const, "BACKOFF_MAX", 0.1)


def entity_id(
    hass: HomeAssistant, platform: str, key: str, agent_id: str = AGENT
) -> str:
    value = er.async_get(hass).async_get_entity_id(
        platform, DOMAIN, f"{agent_id}_{key}"
    )
    assert value is not None, f"no {platform} entity {key} for {agent_id}"
    return value


def link_entity(hass: HomeAssistant, entry_id: str) -> str:
    value = er.async_get(hass).async_get_entity_id(
        "binary_sensor", DOMAIN, f"{entry_id}_link"
    )
    assert value is not None
    return value


def agent_device(hass: HomeAssistant, agent_id: str = AGENT) -> dr.DeviceEntry | None:
    found = dr.async_get(hass).async_get_devices(identifiers={(DOMAIN, agent_id)})
    return found[0] if found else None


def state(hass: HomeAssistant, eid: str) -> str:
    current = hass.states.get(eid)
    assert current is not None, eid
    return current.state


# ------------------------------------------------- AC1, AC6: link and auth


async def test_setup_connects_and_creates_devices(
    hass: HomeAssistant, fake_app: FakeApp
) -> None:
    entry = await setup_entry(hass, fake_app)
    await wait_for(hass, lambda: agent_device(hass) is not None)
    assert state(hass, link_entity(hass, entry.entry_id)) == STATE_ON
    device = agent_device(hass)
    assert device is not None
    assert device.name == "Heating"
    for platform, key in [
        ("sensor", "status"),
        ("sensor", "last_result"),
        ("sensor", "cost_today"),
        ("switch", "enabled"),
        ("ai_task", "ai_task"),
        ("conversation", "conversation"),
    ]:
        eid = entity_id(hass, platform, key)
        assert er.async_get(hass).async_get(eid).device_id == device.id  # type: ignore[union-attr]
    assert state(hass, entity_id(hass, "sensor", "status")) == "idle"
    assert (
        state(hass, entity_id(hass, "sensor", "last_result")) == "The rooms are warm."
    )
    assert float(state(hass, entity_id(hass, "sensor", "cost_today"))) == 0.0125
    assert state(hass, entity_id(hass, "switch", "enabled")) == STATE_ON


async def test_bad_secret_keeps_the_link_off(
    hass: HomeAssistant, fake_app: FakeApp
) -> None:
    entry = await setup_entry(hass, fake_app, secret="another-secret")
    await wait_for(hass, lambda: fake_app.refused >= 2)
    link = hass.states.get(link_entity(hass, entry.entry_id))
    assert link is not None
    assert link.state == STATE_OFF
    assert "refused the secret" in link.attributes["last_error"]
    assert fake_app.connects == 0
    assert agent_device(hass) is None


async def test_link_recovers_after_a_drop(
    hass: HomeAssistant, fake_app: FakeApp
) -> None:
    entry = await setup_entry(hass, fake_app)
    link = link_entity(hass, entry.entry_id)
    await wait_for(hass, lambda: state(hass, link) == STATE_ON)
    status = entity_id(hass, "sensor", "status")
    await fake_app.stop()
    await wait_for(hass, lambda: state(hass, link) == STATE_OFF)
    assert state(hass, status) == STATE_UNAVAILABLE
    await fake_app.start()  # the same port
    await wait_for(hass, lambda: state(hass, link) == STATE_ON)
    await wait_for(hass, lambda: state(hass, status) == "idle")
    assert fake_app.connects == 2


async def test_app_down_at_start_loads_the_entry_with_the_link_off(
    hass: HomeAssistant, fake_app: FakeApp
) -> None:
    await fake_app.stop()
    entry = await setup_entry(hass, fake_app)
    assert entry.state.value == "loaded"
    assert state(hass, link_entity(hass, entry.entry_id)) == STATE_OFF


async def test_calls_fail_while_the_app_is_unreachable(
    hass: HomeAssistant, fake_app: FakeApp
) -> None:
    entry = await setup_entry(hass, fake_app)
    await wait_for(hass, lambda: agent_device(hass) is not None)
    device = agent_device(hass)
    assert device is not None
    await fake_app.stop()
    await wait_for(hass, lambda: not entry.runtime_data.connected)
    assert state(hass, link_entity(hass, entry.entry_id)) == STATE_OFF
    with pytest.raises(HomeAssistantError, match="unreachable"):
        await hass.services.async_call(
            DOMAIN,
            "invoke",
            {"device_id": device.id},
            blocking=True,
            return_response=True,
        )
    with pytest.raises(HomeAssistantError, match="unreachable"):
        await ai_task.async_generate_data(
            hass,
            task_name="t",
            entity_id=entity_id(hass, "ai_task", "ai_task"),
            instructions="x",
        )


# ------------------------------------------- AC2: devices follow the app


async def test_agents_appear_update_and_disappear(
    hass: HomeAssistant, fake_app: FakeApp
) -> None:
    await setup_entry(hass, fake_app)
    await wait_for(hass, lambda: agent_device(hass) is not None)
    conv = entity_id(hass, "conversation", "conversation")

    await fake_app.push(
        {"type": "agent", "agent": agent(id="agent-2", name="Lights", assist=False)}
    )
    await wait_for(hass, lambda: agent_device(hass, "agent-2") is not None)
    entity_id(hass, "sensor", "status", "agent-2")
    assert (
        er.async_get(hass).async_get_entity_id(
            "conversation", DOMAIN, "agent-2_conversation"
        )
        is None
    ), "no conversation entity for an agent without Assist"

    await fake_app.push(
        {"type": "agent", "agent": agent(status="running", running=1, assist=False)}
    )
    await wait_for(
        hass, lambda: state(hass, entity_id(hass, "sensor", "status")) == "running"
    )
    await wait_for(hass, lambda: er.async_get(hass).async_get(conv) is None)
    assert hass.states.get(conv) is None

    await fake_app.push({"type": "agent", "agent": agent(assist=True)})
    await wait_for(
        hass,
        lambda: (
            er.async_get(hass).async_get_entity_id(
                "conversation", DOMAIN, f"{AGENT}_conversation"
            )
            is not None
        ),
    )

    await fake_app.push({"type": "agent_removed", "agent_id": "agent-2"})
    await wait_for(hass, lambda: agent_device(hass, "agent-2") is None)
    assert (
        er.async_get(hass).async_get_entity_id("sensor", DOMAIN, "agent-2_status")
        is None
    )


async def test_device_follows_name_and_version_changes(
    hass: HomeAssistant, fake_app: FakeApp
) -> None:
    await setup_entry(hass, fake_app)
    await wait_for(hass, lambda: agent_device(hass) is not None)
    await fake_app.push({"type": "agent", "agent": agent(name="Warmth", version=4)})

    def renamed() -> bool:
        device = agent_device(hass)
        return device is not None and device.name == "Warmth"

    await wait_for(hass, renamed)
    device = agent_device(hass)
    assert device is not None
    assert device.sw_version == "definition 4"
    # A reconnect with a changed agent updates the device too.
    fake_app.agents = [agent(name="Heat", version=5)]
    await fake_app.drop()
    await wait_for(
        hass, lambda: getattr(agent_device(hass), "sw_version", "") == "definition 5"
    )
    assert agent_device(hass).name == "Heat"  # type: ignore[union-attr]


async def test_hello_after_reconnect_removes_agents_deleted_meanwhile(
    hass: HomeAssistant, fake_app: FakeApp
) -> None:
    fake_app.agents = [agent(), agent(id="agent-2", name="Lights")]
    await setup_entry(hass, fake_app)
    await wait_for(hass, lambda: agent_device(hass, "agent-2") is not None)
    fake_app.agents = [agent()]
    await fake_app.drop()
    await wait_for(hass, lambda: agent_device(hass, "agent-2") is None)
    assert agent_device(hass) is not None


async def test_last_result_sensor_keeps_a_short_state(
    hass: HomeAssistant, fake_app: FakeApp
) -> None:
    await setup_entry(hass, fake_app)
    await wait_for(hass, lambda: agent_device(hass) is not None)
    long_text = "x" * 400
    snap = agent()
    snap["last_result"]["result"] = long_text
    await fake_app.push({"type": "agent", "agent": snap})
    eid = entity_id(hass, "sensor", "last_result")
    await wait_for(hass, lambda: len(state(hass, eid)) == 255)
    current = hass.states.get(eid)
    assert current is not None
    assert current.attributes["result"] == long_text


async def test_switch_turns_the_agent_off(
    hass: HomeAssistant, fake_app: FakeApp
) -> None:
    async def set_enabled(cmd: dict[str, Any]) -> dict[str, Any]:
        await fake_app.push(
            {"type": "agent", "agent": agent(enabled=False, status="off")}
        )
        return {"type": "response", "success": True, "data": {"enabled": False}}

    fake_app.handlers["set_enabled"] = set_enabled
    await setup_entry(hass, fake_app)
    await wait_for(hass, lambda: agent_device(hass) is not None)
    switch = entity_id(hass, "switch", "enabled")
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": switch}, blocking=True
    )
    assert fake_app.commands[-1] == {
        "type": "set_enabled",
        "id": fake_app.commands[-1]["id"],
        "agent_id": AGENT,
        "enabled": False,
    }
    await wait_for(hass, lambda: state(hass, switch) == STATE_OFF)
    assert state(hass, entity_id(hass, "sensor", "status")) == "off"


# ---------------------------------------------------- AC3: hagent.invoke


async def _ready(hass: HomeAssistant, fake_app: FakeApp) -> dr.DeviceEntry:
    await setup_entry(hass, fake_app)
    await wait_for(hass, lambda: agent_device(hass) is not None)
    device = agent_device(hass)
    assert device is not None
    return device


async def test_invoke_returns_the_response_data(
    hass: HomeAssistant, fake_app: FakeApp
) -> None:
    device = await _ready(hass, fake_app)
    response = await hass.services.async_call(
        DOMAIN,
        "invoke",
        {
            "device_id": device.id,
            "data": {"reason": "test"},
            "timeout": 12,
            "on_timeout": "cancel",
        },
        blocking=True,
        return_response=True,
    )
    assert response == messages()["invoke_response"]["data"]
    command = fake_app.commands[-1]
    assert command["type"] == "invoke"
    assert command["agent_id"] == AGENT
    assert command["source"] == "invoke"
    assert command["data"] == {"reason": "test"}
    assert command["wait"] is True
    assert command["timeout"] == 12
    assert command["on_timeout"] == "cancel"


# ------------------------------------------- admin rule (review M1)


async def _non_admin(hass: HomeAssistant) -> User:
    """A user that is not an admin but may control every entity.

    HA's own entity checks let this user through, so only the hagent admin
    rule can refuse the call.
    """
    return await hass.auth.async_create_user("u", group_ids=["system-users"])


def _invokes(app: FakeApp) -> int:
    return sum(1 for cmd in app.commands if cmd["type"] == "invoke")


async def test_invoke_refuses_a_non_admin_user(
    hass: HomeAssistant, fake_app: FakeApp, hass_admin_user: User
) -> None:
    device = await _ready(hass, fake_app)
    user = await _non_admin(hass)
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            "invoke",
            {"device_id": device.id},
            blocking=True,
            return_response=True,
            context=Context(user_id=user.id),
        )
    assert _invokes(fake_app) == 0
    for context in (Context(user_id=hass_admin_user.id), Context()):
        response = await hass.services.async_call(
            DOMAIN,
            "invoke",
            {"device_id": device.id},
            blocking=True,
            return_response=True,
            context=context,
        )
        assert response == messages()["invoke_response"]["data"]
    assert _invokes(fake_app) == 2


async def test_ai_task_refuses_a_non_admin_user(
    hass: HomeAssistant, fake_app: FakeApp, hass_admin_user: User
) -> None:
    await _ready(hass, fake_app)
    task_entity = entity_id(hass, "ai_task", "ai_task")
    user = await _non_admin(hass)
    with pytest.raises(Unauthorized):
        await ai_task.async_generate_data(
            hass,
            task_name="say",
            entity_id=task_entity,
            instructions="Say ok",
            context=Context(user_id=user.id),
        )
    assert _invokes(fake_app) == 0
    for context in (Context(user_id=hass_admin_user.id), None):
        result = await ai_task.async_generate_data(
            hass,
            task_name="say",
            entity_id=task_entity,
            instructions="Say ok",
            context=context,
        )
        assert result.data == "ok"
    assert _invokes(fake_app) == 2


async def test_ai_task_checks_each_caller_in_concurrent_calls(
    hass: HomeAssistant, fake_app: FakeApp, hass_admin_user: User
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow(cmd: dict[str, Any]) -> dict[str, Any]:
        started.set()
        await release.wait()
        return messages()["invoke_response"]

    fake_app.handlers["invoke"] = slow
    await _ready(hass, fake_app)
    task_entity = entity_id(hass, "ai_task", "ai_task")
    user = await _non_admin(hass)

    def call(context: Context) -> asyncio.Task[Any]:
        return hass.async_create_task(
            ai_task.async_generate_data(
                hass,
                task_name="say",
                entity_id=task_entity,
                instructions="Say ok",
                context=context,
            )
        )

    admin = call(Context(user_id=hass_admin_user.id))
    other = call(Context(user_id=user.id))
    # The admin call stays open in the app while the other call runs.
    # wait_for() cannot be used here: it waits for all HA tasks.
    async with asyncio.timeout(5):
        await started.wait()
        with pytest.raises(Unauthorized):
            await other
    assert _invokes(fake_app) == 1
    release.set()
    assert (await admin).data == "ok"
    assert _invokes(fake_app) == 1


async def test_conversation_refuses_a_non_admin_user(
    hass: HomeAssistant, fake_app: FakeApp, hass_admin_user: User
) -> None:
    await _ready(hass, fake_app)
    conv = entity_id(hass, "conversation", "conversation")
    user = await _non_admin(hass)
    result = await conversation.async_converse(
        hass, "hello", None, Context(user_id=user.id), language="en", agent_id=conv
    )
    assert result.response.response_type.value == "error"
    assert "admin" in result.response.speech["plain"]["speech"]
    assert _invokes(fake_app) == 0
    for context in (Context(user_id=hass_admin_user.id), Context()):
        result = await conversation.async_converse(
            hass, "hello", None, context, language="en", agent_id=conv
        )
        assert result.response.response_type.value != "error"
    assert _invokes(fake_app) == 2


async def test_invoke_by_entity_target_and_defaults(
    hass: HomeAssistant, fake_app: FakeApp
) -> None:
    await _ready(hass, fake_app)
    await hass.services.async_call(
        DOMAIN,
        "invoke",
        {"entity_id": entity_id(hass, "sensor", "status")},
        blocking=True,
        return_response=True,
    )
    command = fake_app.commands[-1]
    assert command["agent_id"] == AGENT
    assert command["wait"] is True
    assert command["timeout"] == 30
    assert "on_timeout" not in command, "the app applies the default"


async def test_invoke_without_wait(hass: HomeAssistant, fake_app: FakeApp) -> None:
    async def queued(cmd: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "response",
            "success": True,
            "data": {"invocation_id": "inv-9", "status": "queued"},
        }

    fake_app.handlers["invoke"] = queued
    device = await _ready(hass, fake_app)
    response = await hass.services.async_call(
        DOMAIN,
        "invoke",
        {"device_id": device.id, "wait": False},
        blocking=True,
        return_response=True,
    )
    assert response == {"invocation_id": "inv-9", "status": "queued"}
    assert fake_app.commands[-1]["wait"] is False
    # Without a response request, the action returns nothing.
    assert (
        await hass.services.async_call(
            DOMAIN, "invoke", {"device_id": device.id, "wait": False}, blocking=True
        )
        is None
    )


async def test_invoke_error_status_raises(
    hass: HomeAssistant, fake_app: FakeApp
) -> None:
    async def off(cmd: dict[str, Any]) -> dict[str, Any]:
        reply = messages()["invoke_response"]
        reply["data"].update(
            status="error",
            path=None,
            result=None,
            error={"code": "agent_off", "message": "the agent is turned off"},
        )
        return reply

    fake_app.handlers["invoke"] = off
    device = await _ready(hass, fake_app)
    with pytest.raises(HomeAssistantError, match="agent_off"):
        await hass.services.async_call(
            DOMAIN,
            "invoke",
            {"device_id": device.id},
            blocking=True,
            return_response=True,
        )


async def test_invoke_command_error_raises(
    hass: HomeAssistant, fake_app: FakeApp
) -> None:
    async def timeout(cmd: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "response",
            "success": False,
            "error": {"code": "timeout", "message": "the app cancelled it"},
        }

    fake_app.handlers["invoke"] = timeout
    device = await _ready(hass, fake_app)
    with pytest.raises(HomeAssistantError, match="timeout"):
        await hass.services.async_call(
            DOMAIN,
            "invoke",
            {"device_id": device.id},
            blocking=True,
            return_response=True,
        )


async def test_invoke_fails_at_once_when_the_link_drops(
    hass: HomeAssistant, fake_app: FakeApp
) -> None:
    started = asyncio.Event()

    async def hang(cmd: dict[str, Any]) -> dict[str, Any]:
        started.set()
        await asyncio.sleep(60)
        return messages()["invoke_response"]

    fake_app.handlers["invoke"] = hang
    device = await _ready(hass, fake_app)
    call = hass.async_create_task(
        hass.services.async_call(
            DOMAIN,
            "invoke",
            {"device_id": device.id},
            blocking=True,
            return_response=True,
        )
    )
    await started.wait()
    await fake_app.drop()
    async with asyncio.timeout(5):
        with pytest.raises(HomeAssistantError, match="unreachable"):
            await call


@pytest.mark.parametrize("target", [{}, {"device_id": "missing"}])
async def test_invoke_needs_one_agent_target(
    hass: HomeAssistant, fake_app: FakeApp, target: dict[str, Any]
) -> None:
    await _ready(hass, fake_app)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "invoke", target, blocking=True, return_response=True
        )


async def test_invoke_refuses_two_agents(
    hass: HomeAssistant, fake_app: FakeApp
) -> None:
    fake_app.agents = [agent(), agent(id="agent-2", name="Lights")]
    await setup_entry(hass, fake_app)
    await wait_for(hass, lambda: agent_device(hass, "agent-2") is not None)
    ids = [agent_device(hass).id, agent_device(hass, "agent-2").id]  # type: ignore[union-attr]
    with pytest.raises(ServiceValidationError, match="exactly one"):
        await hass.services.async_call(
            DOMAIN, "invoke", {"device_id": ids}, blocking=True, return_response=True
        )


async def test_result_event_fires_only_for_undelivered_results(
    hass: HomeAssistant, fake_app: FakeApp
) -> None:
    await _ready(hass, fake_app)
    events: list[Event] = []
    hass.bus.async_listen(EVENT_RESULT, events.append)
    delivered = messages()["result"]
    delivered["delivered"] = True
    await fake_app.push(delivered)
    background = messages()["result"]
    await fake_app.push(background)
    await wait_for(hass, lambda: len(events) == 1)
    await asyncio.sleep(0.05)
    assert len(events) == 1
    assert events[0].data == background["result"]


# ------------------------------------------------------ AC4: AI Task


async def test_ai_task_generate_data_with_structure(
    hass: HomeAssistant, fake_app: FakeApp
) -> None:
    async def structured(cmd: dict[str, Any]) -> dict[str, Any]:
        reply = messages()["invoke_response"]
        reply["data"]["result"] = {"answer": 42}
        return reply

    fake_app.handlers["invoke"] = structured
    await _ready(hass, fake_app)
    task_entity = entity_id(hass, "ai_task", "ai_task")
    import probatio  # noqa: PLC0415

    result = await ai_task.async_generate_data(
        hass,
        task_name="count",
        entity_id=task_entity,
        instructions="How many?",
        structure=probatio.Schema({probatio.Required("answer"): int}),
    )
    assert result.data == {"answer": 42}
    command = fake_app.commands[-1]
    assert command["source"] == "ai_task"
    assert command["text"] == "How many?"
    assert command["data"] == {"task_name": "count"}
    assert command["result_schema"]["type"] == "object"
    assert command["result_schema"]["properties"] == {"answer": {"type": "integer"}}
    assert command["on_timeout"] == "cancel"


async def test_ai_task_text_result(hass: HomeAssistant, fake_app: FakeApp) -> None:
    await _ready(hass, fake_app)
    result = await ai_task.async_generate_data(
        hass,
        task_name="say",
        entity_id=entity_id(hass, "ai_task", "ai_task"),
        instructions="Say ok",
    )
    assert result.data == "ok"
    assert "result_schema" not in fake_app.commands[-1]


async def test_ai_task_features(hass: HomeAssistant, fake_app: FakeApp) -> None:
    await _ready(hass, fake_app)
    current = hass.states.get(entity_id(hass, "ai_task", "ai_task"))
    assert current is not None
    features = ai_task.AITaskEntityFeature(current.attributes["supported_features"])
    assert ai_task.AITaskEntityFeature.GENERATE_DATA in features
    assert ai_task.AITaskEntityFeature.SUPPORT_ATTACHMENTS in features
    assert ai_task.AITaskEntityFeature.GENERATE_IMAGE not in features
    # HA Core 2026.10 has no EVALUATE feature; the entity degrades.
    assert EVALUATE is None


async def test_ai_task_attachments_go_through_the_link(
    hass: HomeAssistant, fake_app: FakeApp, tmp_path: Path
) -> None:
    await _ready(hass, fake_app)
    image = tmp_path / "cam.png"
    image.write_bytes(b"\x89PNG-data")
    entity = hass.data[ai_task.DOMAIN].get_entity(entity_id(hass, "ai_task", "ai_task"))
    assert isinstance(entity, HagentAITaskEntity)
    with conversation.async_get_chat_log(hass, _session(hass), None) as chat_log:
        await entity._async_generate_data(
            ai_task.GenDataTask(
                name="look",
                instructions="describe",
                attachments=[
                    conversation.Attachment(
                        media_content_id="media-source://x/cam.png",
                        mime_type="image/png",
                        path=image,
                    )
                ],
            ),
            chat_log,
        )
    [attachment] = fake_app.commands[-1]["attachments"]
    assert attachment == {
        "name": "cam.png",
        "mime_type": "image/png",
        "data": "iVBORy1kYXRh",
    }


def _session(hass: HomeAssistant) -> Any:
    from homeassistant.helpers.chat_session import async_get_chat_session  # noqa: PLC0415

    with async_get_chat_session(hass) as session:
        return session


def test_attachment_limits(tmp_path: Path) -> None:
    exe = tmp_path / "x.exe"
    exe.write_bytes(b"MZ")
    item = conversation.Attachment(
        media_content_id="m", mime_type="application/x-dosexec", path=exe
    )
    with pytest.raises(HagentInvocationError, match="type"):
        read_attachments([item])
    big = tmp_path / "big.pdf"
    big.write_bytes(b"0" * (const.MAX_ATTACHMENT_BYTES + 1))
    item = conversation.Attachment(
        media_content_id="m", mime_type="application/pdf", path=big
    )
    with pytest.raises(HagentInvocationError, match="larger"):
        read_attachments([item])


def test_attachment_size_is_checked_before_the_read(tmp_path: Path) -> None:
    # A sparse file of 64 GiB: a full read would need 64 GiB of memory.
    huge = tmp_path / "huge.pdf"
    with huge.open("wb") as file:
        file.truncate(64 * 1024**3)
    item = conversation.Attachment(
        media_content_id="m", mime_type="application/pdf", path=huge
    )
    with pytest.raises(HagentInvocationError, match="larger"):
        read_attachments([item])


def test_attachment_sizes_add_up(tmp_path: Path) -> None:
    half = const.MAX_ATTACHMENT_BYTES // 2 + 1
    items = []
    for name in ("a.pdf", "b.pdf"):
        path = tmp_path / name
        path.write_bytes(b"0" * half)
        items.append(
            conversation.Attachment(
                media_content_id="m", mime_type="application/pdf", path=path
            )
        )
    assert len(read_attachments(items[:1])) == 1
    with pytest.raises(HagentInvocationError, match="larger"):
        read_attachments(items)


def test_attachment_must_be_a_regular_file(tmp_path: Path) -> None:
    # open() on a FIFO without a writer blocks; the check must refuse it first.
    fifo = tmp_path / "pipe.txt"
    os.mkfifo(fifo)
    item = conversation.Attachment(
        media_content_id="m", mime_type="text/plain", path=fifo
    )
    with pytest.raises(HagentInvocationError, match="regular"):
        read_attachments([item])


def test_structure_schema_is_an_object_schema() -> None:
    import probatio  # noqa: PLC0415

    schema = structure_schema(
        probatio.Schema(
            {
                probatio.Required("a"): str,
                probatio.Optional("b"): probatio.Coerce(float),
            }
        )
    )
    assert schema["type"] == "object"
    assert schema["required"] == ["a"]


# ------------------------------------------------- AC5: conversation


async def test_conversation_sends_assist_invocations(
    hass: HomeAssistant, fake_app: FakeApp
) -> None:
    async def answer(cmd: dict[str, Any]) -> dict[str, Any]:
        reply = messages()["invoke_response"]
        reply["data"]["result"] = f"you said {cmd['text']}"
        return reply

    fake_app.handlers["invoke"] = answer
    await _ready(hass, fake_app)
    conv = entity_id(hass, "conversation", "conversation")
    result = await conversation.async_converse(
        hass, "hello", None, Context(), language="en", agent_id=conv
    )
    assert result.response.speech["plain"]["speech"] == "you said hello"
    command = fake_app.commands[-1]
    assert command["source"] == "assist"
    assert command["conversation_id"] == result.conversation_id
    assert command["data"]["language"] == "en"


async def test_conversation_reports_an_unreachable_app(
    hass: HomeAssistant, fake_app: FakeApp
) -> None:
    await _ready(hass, fake_app)
    conv = entity_id(hass, "conversation", "conversation")
    await fake_app.stop()
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    await wait_for(hass, lambda: not entry.runtime_data.connected)
    result = await conversation.async_converse(
        hass, "hello", None, Context(), language="en", agent_id=conv
    )
    assert result.response.response_type.value == "error"
    assert "unreachable" in result.response.speech["plain"]["speech"]


async def test_unload_stops_the_link(hass: HomeAssistant, fake_app: FakeApp) -> None:
    entry = await setup_entry(hass, fake_app)
    await wait_for(hass, lambda: fake_app.sockets != [])
    assert await hass.config_entries.async_unload(entry.entry_id)
    await wait_for(hass, lambda: fake_app.sockets == [])
