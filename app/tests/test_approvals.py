"""The approval flow: a held call reaches HA only after a valid accept.

Every test uses FakeHA. The held call is ``light.turn_on`` on
``light.kitchen``. ``light_calls`` lists the calls that reached HA for it.
Notify calls are not held calls, so the tests never count all commands.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from hagent_app.agents import Agent
from hagent_app.app import AppContext
from hagent_app.approvals import ApprovalsComponent, ApprovalsConfig
from hagent_app.approvals.notifier import ha_slug
from hagent_app.approvals.service import ACTION_EVENT
from hagent_app.config import Config, ConfigError
from hagent_app.http import ChangeEvents, HttpServer
from hagent_app.tools import ToolResult
from helpers.fake_ha import APP_USER_ID, FakeHA, Message
from helpers.tools import Harness, harness
from helpers.wait import wait_until

APPROVER = "a" * 32
OTHER = "b" * 32
SECOND = "c" * 32
CONTROL_KITCHEN = {"tools__ha": {"level": "control", "scope": {"areas": ["kitchen"]}}}
APPROVE = {**CONTROL_KITCHEN, "approval__services": ["light.turn_on"]}
CALL = {
    "domain": "light",
    "service": "turn_on",
    "target": {"entity_id": "light.kitchen"},
}
SERVICE = "mobile_app_phone_a"


def light_calls(ha: FakeHA) -> list[Message]:
    """The calls of the held service that reached HA."""
    return [
        c
        for c in ha.commands_of("call_service")
        if c.get("domain") == "light" and c.get("service") == "turn_on"
    ]


def notify_calls(ha: FakeHA, *, clear: bool | None = None) -> list[Message]:
    """The notify calls. ``clear`` selects clear or send calls."""
    calls = [c for c in ha.commands_of("call_service") if c.get("domain") == "notify"]
    if clear is None:
        return calls
    return [
        c
        for c in calls
        if (c["service_data"]["message"] == "clear_notification") is clear
    ]


def add_phone(  # noqa: PLR0913
    ha: FakeHA,
    *,
    user_id: str,
    person: str,
    device_id: str,
    name: str,
    service: bool = True,
    link: bool = True,
) -> asyncio.Task[None] | None:
    """Add a mobile_app device, its tracker, and a person that links it."""
    tracker = f"device_tracker.{device_id}"
    ha.registry["entity"].append(
        {"entity_id": tracker, "platform": "mobile_app", "device_id": device_id}
    )
    ha.registry["device"].append(
        {"id": device_id, "name": name, "identifiers": [["mobile_app", device_id]]}
    )
    if service:
        ha.services.setdefault("notify", {})[f"mobile_app_{ha_slug(name)}"] = {}
    if not link:
        return None
    return ha.set_state(
        f"person.{person}",
        "home",
        {"user_id": user_id, "device_trackers": [tracker]},
    )


@dataclass
class Env:
    """A harness with a started approvals component."""

    h: Harness
    approvals: ApprovalsComponent
    events: ChangeEvents
    ctx: AppContext

    @property
    def ha(self) -> FakeHA:
        return self.h.ha

    async def agent(self) -> Agent:
        agent = await self.h.agent(**APPROVE)
        self.h.reset()
        return agent

    def start_call(self, agent: Agent) -> asyncio.Task[ToolResult]:
        return asyncio.create_task(self.h.call(agent, "ha.call_service", CALL))

    async def sent(self, count: int = 1) -> Message:
        """Wait for the notification send. Return its service data."""
        await wait_until(lambda: len(notify_calls(self.ha, clear=False)) >= count)
        return notify_calls(self.ha, clear=False)[count - 1]["service_data"]  # type: ignore[no-any-return]

    async def tap(
        self,
        action: str,
        user_id: str | None,
        event_type: str = ACTION_EVENT,
    ) -> None:
        await self.ha.fire_event(
            event_type, {"action": action}, context={"user_id": user_id}
        )

    async def record(self, request_id: str | None = None) -> Any:
        service = self.approvals.service()
        if request_id is None:
            return (await service.list_requests())[0]
        return await service.get(request_id)

    async def answers(self, count: int) -> list[dict[str, Any]]:
        service = self.approvals.service()
        record = await self.record()
        for _ in range(100):
            found = await service.answers(record.id)
            if len(found) >= count:
                return found
            await asyncio.sleep(0.01)
        return await service.answers(record.id)


def actions(data: Message) -> tuple[str, str]:
    accept, reject = data["data"]["actions"]
    return accept["action"], reject["action"]


@asynccontextmanager
async def approval_env(
    tmp_path: Path, *, phone: bool = True, **options: Any
) -> AsyncIterator[Env]:
    """Start the harness, a phone of APPROVER and the approvals component."""
    options.setdefault("approvers", [APPROVER])
    options.setdefault("timeout_seconds", 5)
    events = ChangeEvents()
    approvals = ApprovalsComponent(events)
    async with harness(tmp_path, approval=approvals.gate) as h:
        if phone:
            task = add_phone(
                h.ha, user_id=APPROVER, person="a", device_id="dev_a", name="Phone A"
            )
            if task is not None:
                await task
            await wait_until(lambda: h.states.get("person.a") is not None)
        config = Config(
            ha_url=h.ha.url,
            ha_token=h.ha.token,
            data_dir=tmp_path,
            port=0,
            options={"approvals": options},
        )
        ctx = AppContext(config, h.db, h.client, h.states, "test")
        await approvals.start(ctx)
        try:
            await wait_until(lambda: approvals._app_user is not None)
            yield Env(h, approvals, events, ctx)
        finally:
            await approvals.stop()


# ----------------------------------------------------------- happy path


async def test_held_call_reaches_ha_only_after_a_valid_accept(tmp_path: Path) -> None:
    async with approval_env(tmp_path) as env:
        agent = await env.agent()
        task = env.start_call(agent)
        data = await env.sent()
        assert notify_calls(env.ha, clear=False)[0]["service"] == SERVICE
        accept, reject = actions(data)
        record = await env.record()
        assert accept == f"HAGENT_ACCEPT_{record.id}"
        assert reject == f"HAGENT_REJECT_{record.id}"
        assert data["data"]["tag"] == f"hagent-approval-{record.id}"
        assert len(record.id) == 32
        # The text names the checked service and entity only.
        assert "light.turn_on" in data["message"]
        assert "light.kitchen" in data["message"]
        await asyncio.sleep(0.05)
        assert light_calls(env.ha) == []
        assert record.status == "pending"

        await env.tap(accept, APPROVER)
        result = await task
        assert result.ok, result.error
        assert len(light_calls(env.ha)) == 1
        record = await env.record(record.id)
        assert record.status == "accepted"
        assert record.answered_by == APPROVER
        assert record.answered_via == "notification"
        assert record.targets == [SERVICE]
        clears = notify_calls(env.ha, clear=True)
        assert [c["service_data"]["data"]["tag"] for c in clears] == [
            f"hagent-approval-{record.id}"
        ]


async def test_reject_never_reaches_ha(tmp_path: Path) -> None:
    async with approval_env(tmp_path) as env:
        task = env.start_call(await env.agent())
        _, reject = actions(await env.sent())
        await env.tap(reject, APPROVER)
        result = await task
        assert result.error_code == "approval_rejected"
        assert light_calls(env.ha) == []
        assert (await env.record()).status == "rejected"
        assert len(notify_calls(env.ha, clear=True)) == 1


async def test_timeout_fails_closed_and_clears(tmp_path: Path) -> None:
    async with approval_env(tmp_path, timeout_seconds=0.3) as env:
        task = env.start_call(await env.agent())
        await env.sent()
        # A body tap fires no action event, so nothing arrives.
        result = await task
        assert result.error_code == "approval_timeout"
        assert light_calls(env.ha) == []
        record = await env.record()
        assert record.status == "timeout"
        assert len(notify_calls(env.ha, clear=True)) == 1


# ------------------------------------------------------- refused answers


@pytest.mark.parametrize(
    ("user_id", "outcome"),
    [(OTHER, "not_approver"), (None, "no_user"), (APP_USER_ID, "app_user")],
)
async def test_answers_from_other_users_have_no_effect(
    tmp_path: Path, user_id: str | None, outcome: str
) -> None:
    # The app user is in the list too: it must still never count.
    approvers = [APPROVER, APP_USER_ID]
    async with approval_env(tmp_path, timeout_seconds=0.5, approvers=approvers) as env:
        task = env.start_call(await env.agent())
        accept, _ = actions(await env.sent())
        await env.tap(accept, user_id)
        result = await task
        assert result.error_code == "approval_timeout"
        assert light_calls(env.ha) == []
        answers = await env.answers(1)
        assert [a["outcome"] for a in answers] == [outcome]
        # The app user is never stored as an approver of the request.
        assert (await env.record()).approvers == [APPROVER]


async def test_legacy_and_malformed_events_are_ignored(tmp_path: Path) -> None:
    async with approval_env(tmp_path, timeout_seconds=0.5) as env:
        task = env.start_call(await env.agent())
        accept, _ = actions(await env.sent())
        await env.tap(accept, APPROVER, event_type="ios.notification_action_fired")
        await env.tap(accept, APPROVER, event_type="ios.action_fired")
        for bad in (
            accept + "0",
            accept.lower(),
            "X" + accept,
            accept.replace("ACCEPT", "ALLOW"),
            accept + "\n",
        ):
            await env.tap(bad, APPROVER)
        await env.ha.fire_event(ACTION_EVENT, {}, context={"user_id": APPROVER})
        result = await task
        assert result.error_code == "approval_timeout"
        assert light_calls(env.ha) == []
        assert await env.approvals.service().answers((await env.record()).id) == []


async def test_answer_for_an_unknown_request_is_ignored(tmp_path: Path) -> None:
    async with approval_env(tmp_path) as env:
        service = env.approvals.service()
        assert await service.answer("0" * 32, "accept", APPROVER, "api") == "unknown"
        assert await service.answer("../x", "accept", APPROVER, "api") == "unknown"


# ------------------------------------------------------------- races


@pytest.mark.parametrize("first", ["accept", "reject"])
async def test_first_valid_answer_wins(tmp_path: Path, first: str) -> None:
    async with approval_env(tmp_path, approvers=[APPROVER, SECOND]) as env:
        task = env.start_call(await env.agent())
        accept, reject = actions(await env.sent())
        order = [accept, reject] if first == "accept" else [reject, accept]
        await env.tap(order[0], APPROVER)
        await env.tap(order[1], SECOND)
        result = await task
        assert result.ok is (first == "accept")
        assert len(light_calls(env.ha)) == (1 if first == "accept" else 0)
        answers = await env.answers(2)
        assert [a["outcome"] for a in answers] == ["applied", "late"]
        record = await env.record()
        assert record.answered_by == APPROVER


async def test_late_answer_after_timeout_is_only_logged(tmp_path: Path) -> None:
    async with approval_env(tmp_path, timeout_seconds=0.2) as env:
        task = env.start_call(await env.agent())
        accept, _ = actions(await env.sent())
        assert (await task).error_code == "approval_timeout"
        await env.tap(accept, APPROVER)
        answers = await env.answers(1)
        assert [a["outcome"] for a in answers] == ["late"]
        await asyncio.sleep(0.05)
        assert light_calls(env.ha) == []
        assert (await env.record()).status == "timeout"


async def test_cancelled_turn_clears_and_ignores_later_accept(tmp_path: Path) -> None:
    async with approval_env(tmp_path) as env:
        task = env.start_call(await env.agent())
        accept, _ = actions(await env.sent())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        record = await env.record()
        assert record.status == "cancelled"
        assert len(notify_calls(env.ha, clear=True)) == 1
        await env.tap(accept, APPROVER)
        assert [a["outcome"] for a in await env.answers(1)] == ["late"]
        assert light_calls(env.ha) == []


async def test_restart_expires_pending_requests(tmp_path: Path) -> None:
    async with approval_env(tmp_path) as env:
        task = env.start_call(await env.agent())
        accept, _ = actions(await env.sent())
        record = await env.record()
        # A crash: a new component starts on the same database, while the
        # old waiter is gone.
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await env.h.db.run(
            lambda c: c.execute(
                "UPDATE approval_requests SET status = 'pending' WHERE id = ?",
                (record.id,),
            )
        )
        fresh = ApprovalsComponent()
        await fresh.start(env.ctx)
        try:
            await wait_until(lambda: fresh._app_user is not None)
            assert (await fresh.service().get(record.id)).status == "expired"  # type: ignore[union-attr]
            await env.tap(accept, APPROVER)
            answers = await env.answers(2)
            assert {a["outcome"] for a in answers} == {"late"}
            assert light_calls(env.ha) == []
        finally:
            await fresh.stop()


async def test_stop_wakes_a_waiting_turn(tmp_path: Path) -> None:
    async with approval_env(tmp_path) as env:
        task = env.start_call(await env.agent())
        await env.sent()
        await env.approvals.stop()
        result = await task
        assert result.error_code == "approval_unavailable"
        assert light_calls(env.ha) == []
        # The gate fails closed after stop.
        result = await env.h.call(await env.agent(), "ha.call_service", CALL)
        assert result.error_code == "approval_unavailable"
        assert notify_calls(env.ha, clear=False) == []


# ------------------------------------------------------------ delivery


async def test_no_approvers_fails_closed_without_a_send(tmp_path: Path) -> None:
    async with approval_env(tmp_path, approvers=[]) as env:
        result = await env.h.call(await env.agent(), "ha.call_service", CALL)
        assert result.error_code == "approval_unavailable"
        assert notify_calls(env.ha) == []
        assert light_calls(env.ha) == []


async def test_unknown_app_user_fails_closed(tmp_path: Path) -> None:
    async with approval_env(tmp_path) as env:
        env.approvals._app_user = None
        result = await env.h.call(await env.agent(), "ha.call_service", CALL)
        assert result.error_code == "approval_unavailable"
        assert notify_calls(env.ha) == []


async def test_duplicate_device_name_fails_closed(tmp_path: Path) -> None:
    async with approval_env(tmp_path) as env:
        # A second registration with the same name: the legacy service can
        # route to either phone.
        add_phone(
            env.ha,
            user_id=OTHER,
            person="b",
            device_id="dev_b",
            name="Phone-A",
            link=False,
        )
        result = await env.h.call(await env.agent(), "ha.call_service", CALL)
        assert result.error_code == "approval_unavailable"
        assert notify_calls(env.ha) == []
        assert light_calls(env.ha) == []
        record = await env.record()
        assert record.status == "failed"
        assert any("matches 2 mobile_app devices" in p for p in record.problems)


async def test_missing_service_fails_closed(tmp_path: Path) -> None:
    async with approval_env(tmp_path) as env:
        del env.ha.services["notify"][SERVICE]
        result = await env.h.call(await env.agent(), "ha.call_service", CALL)
        assert result.error_code == "approval_unavailable"
        assert notify_calls(env.ha) == []
        assert any("does not exist" in p for p in (await env.record()).problems)


async def test_no_person_link_fails_closed(tmp_path: Path) -> None:
    async with approval_env(tmp_path, phone=False) as env:
        result = await env.h.call(await env.agent(), "ha.call_service", CALL)
        assert result.error_code == "approval_unavailable"
        assert notify_calls(env.ha) == []
        assert any("no person entity" in p for p in (await env.record()).problems)


async def test_failed_send_fails_closed(tmp_path: Path) -> None:
    async with approval_env(tmp_path) as env:
        original = env.ha.handlers.get("call_service")
        assert original is None

        def refuse(_ha: FakeHA, msg: Message) -> Any:
            from helpers.fake_ha import FakeCommandError  # noqa: PLC0415

            raise FakeCommandError("service_error", "push failed")

        env.ha.handlers["call_service"] = refuse
        result = await env.h.call(await env.agent(), "ha.call_service", CALL)
        assert result.error_code == "approval_unavailable"
        assert (await env.record()).status == "failed"
        assert light_calls(env.ha) == []


async def test_override_wins_over_the_person_chain(tmp_path: Path) -> None:
    overrides = {APPROVER: ["notify.mobile_app_tablet"]}
    async with approval_env(tmp_path, notify_services=overrides) as env:
        add_phone(
            env.ha,
            user_id=APPROVER,
            person="unused",
            device_id="dev_t",
            name="Tablet",
            link=False,
        )
        task = env.start_call(await env.agent())
        accept, _ = actions(await env.sent())
        assert [c["service"] for c in notify_calls(env.ha)] == ["mobile_app_tablet"]
        await env.tap(accept, APPROVER)
        assert (await task).ok


async def test_wrong_phone_cannot_approve(tmp_path: Path) -> None:
    # A person of APPROVER links the phone of OTHER. The request reaches
    # that phone, but a tap there carries the HA user of OTHER.
    async with approval_env(tmp_path, phone=False, timeout_seconds=0.4) as env:
        env.ha.registry["entity"].append(
            {
                "entity_id": "device_tracker.dev_b",
                "platform": "mobile_app",
                "device_id": "dev_b",
            }
        )
        env.ha.registry["device"].append(
            {"id": "dev_b", "name": "Phone B", "identifiers": [["mobile_app", "b"]]}
        )
        env.ha.services["notify"] = {"mobile_app_phone_b": {}}
        await env.ha.set_state(
            "person.a",
            "home",
            {"user_id": APPROVER, "device_trackers": ["device_tracker.dev_b"]},
        )
        await wait_until(lambda: env.h.states.get("person.a") is not None)
        task = env.start_call(await env.agent())
        accept, _ = actions(await env.sent())
        await env.tap(accept, OTHER)
        assert (await task).error_code == "approval_timeout"
        assert light_calls(env.ha) == []


async def test_dry_run_sends_nothing(tmp_path: Path) -> None:
    async with approval_env(tmp_path) as env:
        result = await env.h.call(
            await env.agent(), "ha.call_service", CALL, dry_run=True
        )
        assert result.ok
        assert result.data["needs_approval"] is True
        assert notify_calls(env.ha) == []


# ----------------------------------------------------------------- config


@pytest.mark.parametrize(
    "options",
    [
        {"approvers": ["not-an-id"]},
        {"approvers": "a" * 32},
        {"timeout_seconds": 0},
        {"timeout_seconds": 4000},
        {"timeout_seconds": True},
        {"approvers": [APPROVER], "notify_services": {OTHER: ["mobile_app_x"]}},
        {"approvers": [APPROVER], "notify_services": {APPROVER: ["light.x"]}},
        {"approvers": [APPROVER], "notify_services": {APPROVER: []}},
        {"approvers": [APPROVER], "notify_services": {APPROVER: ["persistent_x"]}},
    ],
)
def test_config_rejects_bad_values(options: dict[str, Any]) -> None:
    config = Config(
        ha_url="", ha_token="", data_dir=Path(), options={"approvals": options}
    )
    with pytest.raises(ConfigError):
        config.section("approvals", ApprovalsConfig)


# -------------------------------------------------------------------- API


@asynccontextmanager
async def api_env(tmp_path: Path, **options: Any) -> AsyncIterator[tuple[Env, str]]:
    async with approval_env(tmp_path, **options) as env:
        http = HttpServer()
        http.add_routes(env.approvals.routes())
        await http.start(env.ctx)
        try:
            yield env, f"http://127.0.0.1:{http.port}/api/approvals"
        finally:
            await http.stop()


async def test_api_lists_and_answers_pending_requests(tmp_path: Path) -> None:
    async with api_env(tmp_path) as (env, base), aiohttp.ClientSession() as session:
        task = env.start_call(await env.agent())
        await env.sent()
        headers = {"X-Remote-User-Id": APPROVER}
        async with session.get(f"{base}?status=pending", headers=headers) as resp:
            body = await resp.json()
        assert resp.status == 200
        assert body["can_answer"] is True
        [item] = body["approvals"]
        assert item["status"] == "pending"
        assert item["tool"] == "ha.call_service"
        request_id = item["id"]
        async with session.get(f"{base}/{request_id}", headers=headers) as resp:
            assert (await resp.json())["can_answer"] is True
        assert light_calls(env.ha) == []

        url = f"{base}/{request_id}/answer"
        async with session.post(
            url, json={"decision": "accept"}, headers=headers
        ) as resp:
            assert resp.status == 200
            assert (await resp.json())["status"] == "accepted"
        assert (await task).ok
        assert len(light_calls(env.ha)) == 1
        record = await env.record(request_id)
        assert record.answered_via == "api"
        # A second answer conflicts.
        async with session.post(
            url, json={"decision": "reject"}, headers=headers
        ) as resp:
            assert resp.status == 409
            assert (await resp.json())["error"]["code"] == "approval_closed"


@pytest.mark.parametrize(
    ("headers", "status", "code"),
    [
        ({}, 403, "no_user"),
        ({"X-Remote-User-Id": OTHER}, 403, "not_approver"),
        ({"X-Remote-User-Id": APP_USER_ID}, 403, "not_approver"),
    ],
)
async def test_api_refuses_non_approvers(
    tmp_path: Path, headers: dict[str, str], status: int, code: str
) -> None:
    async with (
        api_env(tmp_path, approvers=[APPROVER, APP_USER_ID], timeout_seconds=1) as (
            env,
            base,
        ),
        aiohttp.ClientSession() as session,
    ):
        task = env.start_call(await env.agent())
        await env.sent()
        record = await env.record()
        url = f"{base}/{record.id}/answer"
        async with session.post(
            url, json={"decision": "accept"}, headers=headers
        ) as resp:
            assert resp.status == status
            assert (await resp.json())["error"]["code"] == code
        async with session.get(base, headers=headers) as resp:
            assert (await resp.json())["can_answer"] is False
        assert (await task).error_code == "approval_timeout"
        assert light_calls(env.ha) == []


async def test_api_errors(tmp_path: Path) -> None:
    async with api_env(tmp_path) as (_env, base), aiohttp.ClientSession() as session:
        headers = {"X-Remote-User-Id": APPROVER}
        missing = f"{base}/{'0' * 32}"
        async with session.get(missing) as resp:
            assert resp.status == 404
        async with session.post(
            f"{missing}/answer", json={"decision": "accept"}, headers=headers
        ) as resp:
            assert resp.status == 404
        async with session.post(
            f"{missing}/answer", json={"decision": "yes"}, headers=headers
        ) as resp:
            assert resp.status == 400
        async with session.get(f"{base}?status=nope") as resp:
            assert resp.status == 400
        async with session.get(f"{base}?limit=0") as resp:
            assert resp.status == 400
        async with session.post(
            f"{missing}/answer", data=b"{", headers=headers
        ) as resp:
            assert resp.status == 400


async def test_change_events_are_published(tmp_path: Path) -> None:
    async with approval_env(tmp_path) as env:
        seen: list[tuple[str, str, str]] = []
        env.events.subscribe(
            lambda e: seen.append((e.topic, e.action, e.data["status"]))
        )
        task = env.start_call(await env.agent())
        _, reject = actions(await env.sent())
        await env.tap(reject, APPROVER)
        await task
        assert seen == [
            ("approvals", "created", "pending"),
            ("approvals", "updated", "rejected"),
        ]


def test_message_json_is_stable() -> None:
    # The payload is plain JSON for the notify service.
    from hagent_app.approvals.notifier import _payload  # noqa: PLC0415
    from hagent_app.approvals.service import message_for  # noqa: PLC0415

    payload = _payload(message_for("0" * 32, "text"))
    assert json.loads(json.dumps(payload)) == payload
    assert payload["data"]["actions"][0]["authenticationRequired"] is True
