"""The tool executor with FakeHA and fake stores.

The main test is ``test_out_of_scope_call_never_reaches_ha_or_a_store``. It
runs a table of out-of-scope calls for every registered tool, and fails if a
tool has no case. For each call it proves three things: the executor denies
the call, FakeHA receives no command, and no fake store receives a call.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from hagent_app.agents import Agent
from hagent_app.agents.definition import AgentDefinition
from hagent_app.tools import ALL_TOOLS
from helpers.fake_ha import FakeHA
from helpers.tools import Harness, RecordingGate, harness
from helpers.wait import wait_until

OTHER_STORE = "f" * 32


@pytest.fixture
async def h(tmp_path: Path) -> AsyncIterator[Harness]:
    async with harness(tmp_path) as value:
        yield value


@dataclass(frozen=True)
class Case:
    """One out-of-scope call."""

    tool: str
    args: dict[str, Any]
    code: str
    definition: dict[str, Any] = field(default_factory=dict)
    # "other_store": attach the private store of another agent, read only.
    # "other_owner": the item belongs to another agent.
    setup: str = ""


READ_HALL = {"tools__ha": {"level": "read", "scope": {"entities": ["light.hall"]}}}
CONTROL_KITCHEN = {"tools__ha": {"level": "control", "scope": {"areas": ["kitchen"]}}}
SELF_READ_HALL = {"tools__self": True, **READ_HALL}

ADMIN_LOCK_APPROVAL = {
    "tools__ha": {"level": "admin"},
    "approval": {"entities": ["lock.front"]},
}

# Regressions for review-8 (security review of task-8). Each one was a
# working bypass before the fix.
REVIEW_8_CASES = [
    # Finding 1: a cross-domain service with unscoped selectors in its data.
    Case(
        "ha.call_service",
        {
            "domain": "recorder",
            "service": "purge_entities",
            "target": {"entity_id": "input_boolean.guest"},
            "data": {"entity_globs": ["*"], "keep_days": 0},
        },
        "indirect_service",
        {"outputs": ["input_boolean.guest"]},
    ),
    Case(
        "ha.call_service",
        {
            "domain": "recorder",
            "service": "purge_entities",
            "target": {"entity_id": "light.hall"},
            "data": {"entity_globs": ["*"], "domains": ["lock", "sensor"]},
        },
        # The scope names the service, but it declares no entity target.
        "target_not_declared",
        {
            "tools__ha": {
                "level": "control",
                "scope": {"entities": ["light.hall"]},
                "services": ["recorder.purge_entities"],
            }
        },
    ),
    Case(
        "ha.call_service",
        {
            "domain": "zwave_js",
            "service": "multicast_set_value",
            "target": {"entity_id": "light.hall"},
            "data": {
                "broadcast": True,
                "command_class": 37,
                "property": "targetValue",
                "value": True,
            },
        },
        "indirect_service",
        {"outputs": ["light.hall"]},
    ),
    Case(
        "ha.call_service",
        {
            "domain": "remote_bridge",
            "service": "send",
            "target": {"entity_id": "light.hall"},
            "data": {"command": "x"},
        },
        "cross_domain",
        {"outputs": ["light.hall"]},
    ),
    Case(
        "ha.call_service",
        {
            "domain": "light",
            "service": "turn_on",
            "target": {"entity_id": "light.kitchen"},
            "data": {"entity_globs": ["*"]},
        },
        "unknown_field",
        CONTROL_KITCHEN,
    ),
    # Finding 2: group.set and group.remove on a group outside the scope.
    Case(
        "ha.call_service",
        {
            "domain": "group",
            "service": "set",
            "data": {"object_id": "everyone", "entities": ["light.hall"]},
        },
        "out_of_scope",
        {
            "tools__ha": {
                "level": "control",
                "scope": {"entities": ["light.hall"]},
                "services": ["group.set", "group.remove"],
            }
        },
    ),
    Case(
        "ha.call_service",
        {"domain": "group", "service": "remove", "data": {"object_id": "everyone"}},
        "out_of_scope",
        {
            "tools__ha": {
                "level": "control",
                "scope": {"entities": ["light.hall"]},
                "services": ["group.set", "group.remove"],
            }
        },
    ),
    # Finding 3: an empty or stale state cache.
    Case(
        "ha.call_service",
        {
            "domain": "light",
            "service": "turn_on",
            "target": {"entity_id": "light.hall"},
        },
        "states_unavailable",
        {"tools__ha": {"level": "control", "scope": {"entities": ["light.hall"]}}},
        setup="stale_states",
    ),
    Case(
        "ha.get_state",
        {"entity_id": "light.hall"},
        "states_unavailable",
        READ_HALL,
        setup="stale_states",
    ),
    Case(
        "ha.find_entities",
        {},
        "states_unavailable",
        READ_HALL,
        setup="stale_states",
    ),
    # Finding 4: rule and schedule specs, and unchecked subscription keys.
    Case(
        "self.create",
        {
            "kind": "rule",
            "spec": {
                "trigger": {"entity_id": "lock.front"},
                "action": {
                    "service": "lock.unlock",
                    "target": {"entity_id": "lock.front"},
                },
            },
        },
        "not_supported",
        SELF_READ_HALL,
    ),
    Case(
        "self.create",
        {"kind": "schedule", "spec": {"cron": "* * * * *"}},
        "not_supported",
        SELF_READ_HALL,
    ),
    Case(
        "self.update",
        {"kind": "rule", "item_id": "r1", "spec": {}},
        "not_supported",
        SELF_READ_HALL,
        setup="own_item_r1",
    ),
    Case(
        "self.create",
        {
            "kind": "subscription",
            "spec": {"entity_ids": ["light.hall"], "entity_globs": ["*"]},
        },
        "invalid_spec",
        SELF_READ_HALL,
    ),
    Case(
        "self.create",
        {
            "kind": "subscription",
            "spec": {"entity_ids": ["light.hall"], "domains": ["lock"]},
        },
        "invalid_spec",
        SELF_READ_HALL,
    ),
    # Finding 6: admin approval by entity, through a group and through data.
    Case(
        "ha.call_service",
        {"domain": "lock", "service": "unlock", "target": {"entity_id": "group.locks"}},
        "approval_unavailable",
        ADMIN_LOCK_APPROVAL,
        setup="group_locks",
    ),
    Case(
        "ha.call_service",
        {
            "domain": "scene",
            "service": "apply",
            "data": {"entities": {"lock.front": {"state": "unlocked"}}},
        },
        "approval_unavailable",
        ADMIN_LOCK_APPROVAL,
    ),
    Case(
        "ha.call_service",
        {
            "domain": "scene",
            "service": "apply",
            "data": {"entities": {"{{ 'lock.front' }}": {"state": "unlocked"}}},
        },
        "approval_unavailable",
        ADMIN_LOCK_APPROVAL,
    ),
    *(
        Case(
            "ha.call_service",
            {"domain": "lock", "service": "unlock", key: {"entity_id": value}},
            "approval_unavailable",
            ADMIN_LOCK_APPROVAL,
        )
        for key in ("target", "data")
        for value in ("all", "LOCK.FRONT", "lock.front, lock.back", " lock.front")
    ),
    Case(
        "ha.call_service",
        {
            "domain": "scene",
            "service": "apply",
            "data": {"x": {"entity_id": "all"}},
        },
        "approval_unavailable",
        ADMIN_LOCK_APPROVAL,
    ),
    # Finding 7: an unexpected error in the check.
    Case("memory.rule_stats", {"rule_id": "r1"}, "check_failed", setup="boom"),
]

CASES = [
    # ha
    Case("ha.get_state", {"entity_id": "light.kitchen"}, "out_of_scope", READ_HALL),
    Case("ha.get_state", {"entity_id": "light.hall"}, "tool_off"),
    Case("ha.find_entities", {"query": "hall"}, "tool_off"),
    Case("ha.find_entities", {"domain": "Light"}, "invalid_arguments", READ_HALL),
    Case(
        "ha.get_history",
        {"entity_ids": ["light.hall", "lock.front"], "start": "2026-11-01T00:00:00Z"},
        "out_of_scope",
        READ_HALL,
    ),
    Case(
        "ha.get_statistics",
        {"statistic_ids": ["light.kitchen"], "start": "2026-11-01T00:00:00Z"},
        "out_of_scope",
        READ_HALL,
    ),
    Case(
        "ha.get_statistics",
        {"statistic_ids": ["sensor:external"], "start": "2026-11-01T00:00:00Z"},
        "invalid_arguments",
        READ_HALL,
    ),
    Case(
        "ha.call_service",
        {
            "domain": "light",
            "service": "turn_on",
            "target": {"entity_id": "light.hall"},
        },
        "out_of_scope",
        CONTROL_KITCHEN,
    ),
    Case(
        "ha.call_service",
        {"domain": "light", "service": "turn_on", "target": {"area_id": "lounge"}},
        "out_of_scope",
        CONTROL_KITCHEN,
    ),
    Case(
        "ha.call_service",
        {"domain": "light", "service": "turn_off", "target": {"floor_id": "ground"}},
        "out_of_scope",
        CONTROL_KITCHEN,
    ),
    Case(
        "ha.call_service",
        {
            "domain": "light",
            "service": "turn_on",
            "target": {"entity_id": "light.kitchen"},
            "data": {"flash": "lock.front"},
        },
        "out_of_scope",
        CONTROL_KITCHEN,
    ),
    Case(
        "ha.call_service",
        {"domain": "light", "service": "turn_on", "target": {"area_id": "kitchen"}},
        "out_of_scope",
        CONTROL_KITCHEN,
        setup="group",
    ),
    Case(
        "ha.call_service",
        {"domain": "script", "service": "morning"},
        "indirect_service",
        {"tools__ha": {"level": "control", "scope": {"all": True}}},
    ),
    Case(
        "ha.call_service",
        {"domain": "conversation", "service": "process", "data": {"text": "t"}},
        "no_target",
        {"tools__ha": {"level": "control", "scope": {"all": True}}},
    ),
    Case(
        "ha.call_service",
        {
            "domain": "light",
            "service": "turn_on",
            "target": {"entity_id": "light.hall"},
        },
        "tool_off",
        READ_HALL,
    ),
    Case(
        "ha.admin_read",
        {"command": {"type": "config/entity_registry/list"}},
        "tool_off",
        CONTROL_KITCHEN,
    ),
    Case(
        "ha.admin_read",
        {"command": {"type": "auth/current_user"}},
        "command_not_permitted",
        {"tools__ha": {"level": "admin"}},
    ),
    Case(
        "ha.admin_change",
        {"command": {"type": "input_boolean/delete", "input_boolean_id": "guest"}},
        "tool_off",
        CONTROL_KITCHEN,
    ),
    Case(
        "ha.admin_change",
        {"command": {"type": "execute_script", "sequence": []}},
        "command_not_permitted",
        {"tools__ha": {"level": "admin"}},
    ),
    # knowledge
    Case("knowledge.list", {"store_id": OTHER_STORE}, "store_not_attached"),
    Case(
        "knowledge.read",
        {"store_id": OTHER_STORE, "path": "MAIN.md"},
        "store_not_attached",
    ),
    Case(
        "knowledge.read",
        {"store_id": "<own>", "path": "notes/../../other/MAIN.md"},
        "invalid_arguments",
    ),
    Case(
        "knowledge.read",
        {"store_id": "<own>", "path": "a//b.md"},
        "invalid_arguments",
    ),
    Case(
        "knowledge.write",
        {"store_id": "<own>", "path": "..\\x.md", "mode": "create", "content": "x"},
        "invalid_arguments",
    ),
    Case(
        "knowledge.delete",
        {"store_id": "<own>", "path": "./MAIN.md"},
        "invalid_arguments",
    ),
    Case(
        "knowledge.search",
        {"query": "boiler", "store_ids": [OTHER_STORE]},
        "store_not_attached",
    ),
    Case(
        "knowledge.write",
        {"store_id": "<other>", "path": "a.md", "mode": "create", "content": "x"},
        "read_only",
        setup="other_store",
    ),
    Case(
        "knowledge.write",
        {"store_id": OTHER_STORE, "path": "a.md", "mode": "create", "content": "x"},
        "store_not_attached",
    ),
    Case(
        "knowledge.delete",
        {"store_id": "<other>", "path": "a.md"},
        "read_only",
        setup="other_store",
    ),
    Case("knowledge.list", {}, "tool_off", {"tools__knowledge": False}),
    # memory
    Case(
        "memory.search",
        {"query": "heating", "agent_id": "<other agent>"},
        "invalid_arguments",
    ),
    Case("memory.search", {"query": "heating"}, "tool_off", {"tools__memory": False}),
    Case("memory.rule_stats", {"rule_id": "r1"}, "not_owner", setup="other_owner"),
    # self
    Case("self.list", {"kind": "rule"}, "tool_off"),
    Case(
        "self.list",
        {"kind": "rule", "agent_id": "<other agent>"},
        "invalid_arguments",
        {"tools__self": True},
    ),
    Case(
        "self.create",
        {"kind": "subscription", "spec": {"entity_ids": ["lock.front"]}},
        "out_of_scope",
        SELF_READ_HALL,
    ),
    Case(
        "self.create",
        {"kind": "subscription", "spec": {"event_type": "call_service"}},
        "event_not_permitted",
        SELF_READ_HALL,
    ),
    Case(
        "self.update",
        {
            "kind": "subscription",
            "item_id": "r1",
            "spec": {"entity_ids": ["light.hall"]},
        },
        "not_owner",
        SELF_READ_HALL,
        setup="other_owner",
    ),
    Case(
        "self.update",
        {
            "kind": "subscription",
            "item_id": "s1",
            "spec": {"entity_ids": ["light.kitchen"]},
        },
        "out_of_scope",
        SELF_READ_HALL,
        setup="own_item",
    ),
    Case(
        "self.set_enabled",
        {"kind": "schedule", "item_id": "r1", "enabled": False},
        "not_owner",
        {"tools__self": True},
        setup="other_owner",
    ),
    Case(
        "self.delete",
        {"kind": "rule", "item_id": "r1"},
        "not_owner",
        {"tools__self": True},
        setup="other_owner",
    ),
    # Unknown tool names.
    Case("ha.run_anything", {}, "unknown_tool"),
    *REVIEW_8_CASES,
]


# Tools that cannot get a scope denial, with the test that proves their
# limit.
# ha.find_entities also filters (test_find_entities_returns_only_the_read_scope),
# but it has a denial case: states_unavailable.
FILTER_TOOLS = {
    # These take no agent or item ID: the executor binds the agent ID, so
    # the only out-of-scope form is an extra argument (in CASES).
    "memory.search": "test_memory_and_self_use_the_invocation_agent",
    "self.list": "test_memory_and_self_use_the_invocation_agent",
}
NOT_SCOPE_CODES = {"tool_off", "invalid_arguments", "unknown_tool"}


def test_every_tool_has_an_out_of_scope_case() -> None:
    names = {tool.name for tool in ALL_TOOLS}
    covered = {case.tool for case in CASES if case.code not in NOT_SCOPE_CODES}
    assert names - covered == set(FILTER_TOOLS)
    for test in FILTER_TOOLS.values():
        assert test in globals()


async def _agent_for(h: Harness, case: Case) -> Agent:
    changes = dict(case.definition)
    if case.setup == "other_store":
        other = await h.agent(name="Other")
        assert other.private_store is not None
        own = await h.agent(name="Probe")
        assert own.private_store is not None
        knowledge = [
            {"store_id": own.private_store.id, "access": "read_write"},
            {"store_id": other.private_store.id, "access": "read"},
        ]
        definition = own.definition.to_data()
        definition["knowledge"] = knowledge
        return await h.agents.update(
            own.id, AgentDefinition.from_json(json.dumps(definition))
        )
    if case.setup == "group":
        # A light group in the kitchen with a member in the lounge.
        h.ha.registry["entity"].append(
            {"entity_id": "light.kitchen_group", "area_id": "kitchen", "labels": []}
        )
        await h.ha.set_state(
            "light.kitchen_group",
            "off",
            {"entity_id": ["light.kitchen", "light.lounge"]},
        )
        count = h.registry.reload_count
        await h.ha.registry_updated("entity")
        await wait_until(
            lambda: (
                h.registry.reload_count > count
                and h.states.get("light.kitchen_group") is not None
            )
        )
    if case.setup == "group_locks":
        await h.ha.set_state("group.locks", "locked", {"entity_id": ["lock.front"]})
        await wait_until(lambda: h.states.get("group.locks") is not None)
    agent = await h.agent(**changes)
    if case.setup == "stale_states":
        # The registry stays current. Only the state cache is not in sync,
        # as after a reconnect before the state reload ends.
        h.states._synced = False
        assert not h.registry.stale
    if case.setup == "own_item_r1":
        h.agent_state.owners["r1"] = agent.id
    if case.setup == "boom":

        async def boom(rule_id: str) -> str:
            msg = "db locked"
            raise RuntimeError(msg)

        h.history.rule_owner = boom  # type: ignore[method-assign]
    if case.setup == "other_owner":
        h.history.owners["r1"] = "another-agent"
        h.agent_state.owners["r1"] = "another-agent"
    if case.setup == "own_item":
        h.agent_state.owners["s1"] = agent.id
    return agent


def _resolve_args(agent: Agent, args: dict[str, Any]) -> dict[str, Any]:
    if args.get("store_id") == "<own>":
        assert agent.private_store is not None
        return {**args, "store_id": agent.private_store.id}
    if args.get("store_id") == "<other>":
        other = next(
            a.store_id
            for a in agent.definition.knowledge
            if agent.private_store is None or a.store_id != agent.private_store.id
        )
        return {**args, "store_id": other}
    return args


@pytest.mark.parametrize("case", CASES, ids=lambda c: f"{c.tool}-{c.code}")
async def test_out_of_scope_call_never_reaches_ha_or_a_store(
    h: Harness, case: Case
) -> None:
    agent = await _agent_for(h, case)
    args = _resolve_args(agent, case.args)
    h.reset()
    result = await h.call(agent, case.tool, args)
    assert not result.ok
    assert result.error_code == case.code, result.error
    assert h.ha.ha_commands() == []
    assert h.store_calls() == []
    # Every denied call is in the audit log.
    entries = await h.audit.entries(agent.id)
    assert entries[0].decision == "denied"
    assert entries[0].reason_code == case.code
    assert entries[0].tool == case.tool


# ----------------------------------------------------- permitted calls


async def test_permitted_service_call_sends_explicit_entities(h: Harness) -> None:
    agent = await h.agent(**CONTROL_KITCHEN)
    h.reset()
    result = await h.call(
        agent,
        "ha.call_service",
        {"domain": "light", "service": "turn_on", "target": {"area_id": "kitchen"}},
    )
    assert result.ok, result.error
    [command] = h.ha.commands_of("call_service")
    assert command["target"] == {"entity_id": ["light.kitchen"]}
    entry = (await h.audit.entries(agent.id))[0]
    assert entry.decision == "allowed"
    assert entry.status == "ok"
    assert entry.context_id == result.data["context_id"]
    assert entry.request["target"] == {"entity_id": ["light.kitchen"]}


async def test_output_entity_can_be_set_without_control(h: Harness) -> None:
    agent = await h.agent(outputs=["input_boolean.guest"])
    result = await h.call(
        agent,
        "ha.call_service",
        {
            "domain": "input_boolean",
            "service": "turn_on",
            "target": {"entity_id": "input_boolean.guest"},
        },
    )
    assert result.ok, result.error
    assert h.ha.states["input_boolean.guest"]["state"] == "on"
    entry = (await h.audit.entries(agent.id))[0]
    assert entry.level == "outputs"


async def test_history_reply_is_limited_to_the_checked_entities(h: Harness) -> None:
    agent = await h.agent(**READ_HALL)
    h.reset()
    result = await h.call(
        agent,
        "ha.get_history",
        {"entity_ids": ["light.hall"], "start": "2026-11-01T00:00:00Z"},
    )
    assert result.ok, result.error
    [command] = h.ha.ha_commands()
    assert command["entity_ids"] == ["light.hall"]
    # FakeHA replies with every entity. The tool drops the others.
    assert set(result.data) == {"light.hall"}


async def test_find_entities_returns_only_the_read_scope(h: Harness) -> None:
    agent = await h.agent(tools__ha={"level": "read", "scope": {"areas": ["kitchen"]}})
    result = await h.call(agent, "ha.find_entities", {})
    assert result.ok, result.error
    assert {r["entity_id"] for r in result.data} == {"light.kitchen", "switch.kettle"}
    result = await h.call(agent, "ha.find_entities", {"area_id": "lounge"})
    assert result.data == []


async def test_get_state_reads_the_cache(h: Harness) -> None:
    agent = await h.agent(**READ_HALL)
    result = await h.call(agent, "ha.get_state", {"entity_id": "light.hall"})
    assert result.ok
    assert result.data["state"] == "off"


async def test_knowledge_search_passes_only_attached_stores(h: Harness) -> None:
    agent = await h.agent()
    assert agent.private_store is not None
    result = await h.call(agent, "knowledge.search", {"query": "boiler"})
    assert result.ok, result.error
    assert h.knowledge.calls == [
        ("search", (agent.private_store.id,), "boiler", agent.id)
    ]
    result = await h.call(
        agent,
        "knowledge.write",
        {
            "store_id": agent.private_store.id,
            "path": "notes.md",
            "mode": "create",
            "content": "x",
        },
    )
    assert result.ok
    assert h.knowledge.calls[-1] == (
        "write",
        agent.private_store.id,
        "notes.md",
        agent.id,
    )


async def test_memory_and_self_use_the_invocation_agent(h: Harness) -> None:
    agent = await h.agent(tools__self=True)
    h.history.owners["r1"] = agent.id
    h.agent_state.owners["r1"] = agent.id
    assert (await h.call(agent, "memory.search", {"query": "q"})).ok
    assert (await h.call(agent, "memory.rule_stats", {"rule_id": "r1"})).ok
    assert (await h.call(agent, "self.delete", {"kind": "rule", "item_id": "r1"})).ok
    agent_ids = {call[1] for call in h.store_calls()}
    assert agent_ids == {agent.id}


async def test_store_tools_report_a_missing_store(tmp_path: Path) -> None:
    async with harness(tmp_path) as h:
        object.__setattr__(h.executor._env, "knowledge", None)
        agent = await h.agent()
        result = await h.call(agent, "knowledge.list", {})
        assert not result.ok
        assert result.error_code == "unavailable"


# ------------------------------------------------------------ admin


async def test_admin_change_keeps_the_previous_version(h: Harness) -> None:
    old = {"id": "guest", "name": "Guest mode", "icon": None}
    h.ha.handlers["input_boolean/list"] = lambda _ha, _msg: [old]
    h.ha.handlers["input_boolean/update"] = lambda _ha, msg: {
        **old,
        "name": msg["name"],
    }
    agent = await h.agent(tools__ha={"level": "admin"})
    result = await h.call(
        agent,
        "ha.admin_change",
        {
            "command": {
                "type": "input_boolean/update",
                "input_boolean_id": "guest",
                "name": "Guests",
            }
        },
    )
    assert result.ok, result.error
    entry = (await h.audit.entries(agent.id))[0]
    assert entry.previous == old
    assert entry.status == "ok"
    assert entry.level == "admin"


async def test_admin_change_fails_closed_without_previous_version(h: Harness) -> None:
    agent = await h.agent(tools__ha={"level": "admin"})
    # FakeHA has no handler for input_number/list, so the reader fails.
    h.reset()
    result = await h.call(
        agent,
        "ha.admin_change",
        {"command": {"type": "input_number/delete", "input_number_id": "x"}},
    )
    assert result.error_code == "previous_unavailable"
    assert h.ha.commands_of("input_number/delete") == []


async def test_admin_service_call_keeps_previous_states(h: Harness) -> None:
    agent = await h.agent(tools__ha={"level": "admin"})
    result = await h.call(
        agent,
        "ha.call_service",
        {"domain": "light", "service": "turn_on", "target": {"area_id": "kitchen"}},
    )
    assert result.ok, result.error
    entry = (await h.audit.entries(agent.id))[0]
    assert set(entry.previous["states"]) == {"light.kitchen", "switch.kettle"}
    assert entry.previous["states"]["light.kitchen"]["state"] == "off"
    # Admin is blanket: the call goes to HA unchanged.
    [command] = h.ha.commands_of("call_service")
    assert command["target"] == {"area_id": "kitchen"}


async def test_admin_read_returns_the_reply(h: Harness) -> None:
    agent = await h.agent(tools__ha={"level": "admin"})
    result = await h.call(
        agent, "ha.admin_read", {"command": {"type": "config/floor_registry/list"}}
    )
    assert result.ok
    assert {f["floor_id"] for f in result.data} == {"ground", "upstairs"}


# ------------------------------------------- audit, dry run, approval


async def test_no_ha_call_when_the_audit_write_fails(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*_args: Any, **_kwargs: Any) -> int:
        raise OSError(28, "disk full")

    monkeypatch.setattr(h.audit, "begin", broken)
    agent = await h.agent(**CONTROL_KITCHEN)
    h.reset()
    result = await h.call(
        agent,
        "ha.call_service",
        {
            "domain": "light",
            "service": "turn_on",
            "target": {"entity_id": "light.kitchen"},
        },
    )
    assert result.error_code == "audit_failed"
    assert h.ha.ha_commands() == []


async def test_failed_ha_call_is_recorded(h: Harness) -> None:
    from helpers.fake_ha import FakeCommandError  # noqa: PLC0415

    def fail(_ha: FakeHA, _msg: dict[str, Any]) -> None:
        raise FakeCommandError("home_assistant_error", "device offline")

    h.ha.handlers["call_service"] = fail
    agent = await h.agent(**CONTROL_KITCHEN)
    result = await h.call(
        agent,
        "ha.call_service",
        {
            "domain": "light",
            "service": "turn_on",
            "target": {"entity_id": "light.kitchen"},
        },
    )
    assert result.error_code == "ha_error"
    entry = (await h.audit.entries(agent.id))[0]
    assert entry.status == "error"
    assert "device offline" in (entry.error or "")


async def test_dry_run_checks_and_records_but_does_not_call(h: Harness) -> None:
    agent = await h.agent(**CONTROL_KITCHEN)
    h.reset()
    args = {
        "domain": "light",
        "service": "turn_on",
        "target": {"entity_id": "light.kitchen"},
    }
    result = await h.call(agent, "ha.call_service", args, dry_run=True)
    assert result.ok
    assert result.data["dry_run"] is True
    assert h.ha.ha_commands() == []
    assert (await h.audit.entries(agent.id))[0].status == "dry_run"
    # A dry run still denies an out-of-scope call.
    args["target"] = {"entity_id": "light.hall"}
    result = await h.call(agent, "ha.call_service", args, dry_run=True)
    assert result.error_code == "out_of_scope"


APPROVE = {
    **CONTROL_KITCHEN,
    "approval__services": ["light.turn_on"],
}
CALL = {
    "domain": "light",
    "service": "turn_on",
    "target": {"entity_id": "light.kitchen"},
}


async def test_approval_without_a_gate_is_denied(h: Harness) -> None:
    agent = await h.agent(**APPROVE)
    h.reset()
    result = await h.call(agent, "ha.call_service", CALL)
    assert result.error_code == "approval_unavailable"
    assert h.ha.ha_commands() == []
    # A call that needs no approval still works.
    result = await h.call(
        agent,
        "ha.call_service",
        {**CALL, "service": "turn_off"},
    )
    assert result.ok


@pytest.mark.parametrize("answer", [True, False])
async def test_approval_gate_decides(tmp_path: Path, answer: bool) -> None:
    gate = RecordingGate(answer)
    async with harness(tmp_path, approval=gate) as h:
        agent = await h.agent(
            **{**CONTROL_KITCHEN, "approval__entities": ["light.kitchen"]}
        )
        h.reset()
        result = await h.call(agent, "ha.call_service", CALL)
        assert gate.requests == ["ha.call_service"]
        assert result.ok is answer
        assert bool(h.ha.commands_of("call_service")) is answer
        if not answer:
            assert result.error_code == "approval_rejected"


async def test_dry_run_sends_no_approval_request(tmp_path: Path) -> None:
    gate = RecordingGate(True)
    async with harness(tmp_path, approval=gate) as h:
        agent = await h.agent(**APPROVE)
        h.reset()
        result = await h.call(agent, "ha.call_service", CALL, dry_run=True)
        assert result.ok
        assert result.data["needs_approval"] is True
        assert gate.requests == []
        assert h.ha.ha_commands() == []


async def test_entity_approval_does_not_hold_reads(h: Harness) -> None:
    agent = await h.agent(
        **{**CONTROL_KITCHEN, "approval__entities": ["light.kitchen"]}
    )
    result = await h.call(
        agent,
        "ha.get_history",
        {"entity_ids": ["light.kitchen"], "start": "2026-11-01T00:00:00Z"},
    )
    assert result.ok, result.error


async def test_cancelled_call_finishes_its_audit_row(h: Harness) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def hang(_ha: FakeHA, _msg: dict[str, Any]) -> None:
        started.set()
        await release.wait()

    h.ha.handlers["call_service"] = hang
    agent = await h.agent(**CONTROL_KITCHEN)
    task = asyncio.create_task(h.call(agent, "ha.call_service", CALL))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    entry = (await h.audit.entries(agent.id))[0]
    release.set()
    assert entry.status == "error"
    assert entry.error == "the invocation was cancelled"


async def test_approval_of_a_tool_group(h: Harness) -> None:
    agent = await h.agent(tools__self=True, approval__tools=["self.*"])
    result = await h.call(agent, "self.list", {"kind": "rule"})
    assert result.error_code == "approval_unavailable"
    assert h.store_calls() == []


# --------------------------------------------------------- registry


async def test_stale_registry_denies_and_reload_resolves_again(h: Harness) -> None:
    agent = await h.agent(**CONTROL_KITCHEN)
    hall = {
        "domain": "light",
        "service": "turn_on",
        "target": {"entity_id": "light.hall"},
    }
    assert (await h.call(agent, "ha.call_service", hall)).error_code == "out_of_scope"

    # The user moves light.hall into the kitchen.
    for entry in h.ha.registry["entity"]:
        if entry["entity_id"] == "light.hall":
            entry["area_id"] = "kitchen"
    count = h.registry.reload_count
    await h.ha.registry_updated("entity")
    await wait_until(lambda: h.registry.reload_count > count)
    h.reset()
    result = await h.call(agent, "ha.call_service", hall)
    assert result.ok, result.error

    # HA goes away: the registry is stale and the check fails closed.
    await h.ha.restart()
    await wait_until(lambda: h.registry.stale)
    h.reset()
    result = await h.call(agent, "ha.call_service", hall)
    assert result.error_code == "registry_unavailable"
    assert h.ha.ha_commands() == []


async def test_registry_stays_stale_until_the_reload_ends(h: Harness) -> None:
    """An update event marks the snapshot stale at once, before the reload."""
    release = asyncio.Event()

    async def slow_list(_ha: FakeHA, _msg: dict[str, Any]) -> list[Any]:
        await release.wait()
        return list(h.ha.registry["entity"])

    h.ha.handlers["config/entity_registry/list"] = slow_list
    agent = await h.agent(**CONTROL_KITCHEN)
    await h.ha.registry_updated("entity")
    await wait_until(lambda: h.registry.stale)
    h.reset()
    result = await h.call(agent, "ha.call_service", CALL)
    assert result.error_code == "registry_unavailable"
    assert h.ha.commands_of("call_service") == []
    release.set()
    await wait_until(lambda: not h.registry.stale)
    assert (await h.call(agent, "ha.call_service", CALL)).ok


# ------------------------------------------------ review-8 regressions


async def test_subscription_stores_the_checked_spec(h: Harness) -> None:
    agent = await h.agent(**SELF_READ_HALL)
    result = await h.call(
        agent,
        "self.create",
        {"kind": "subscription", "spec": {"entity_ids": ["light.hall"]}},
    )
    assert result.ok, result.error
    assert h.agent_state.specs == [
        {
            "event_type": "state_changed",
            "entity_ids": ["light.hall"],
            "include_own_writes": False,
        }
    ]


async def test_admin_event_subscription_is_strict(h: Harness) -> None:
    agent = await h.agent(tools__self=True, tools__ha={"level": "admin"})
    spec = {"event_type": "call_service", "debounce_seconds": 5}
    result = await h.call(agent, "self.create", {"kind": "subscription", "spec": spec})
    assert result.ok, result.error
    spec = {"event_type": "call_service", "entity_globs": ["*"]}
    result = await h.call(agent, "self.create", {"kind": "subscription", "spec": spec})
    assert result.error_code == "invalid_spec"


async def test_admin_scan_failure_needs_approval_only_with_entity_approval(
    h: Harness,
) -> None:
    # Without approval.entities, a template in admin data does not hold the call.
    agent = await h.agent(tools__ha={"level": "admin"})
    data = {"entities": {"{{ 'lock.front' }}": {"state": "unlocked"}}}
    result = await h.call(
        agent, "ha.call_service", {"domain": "scene", "service": "apply", "data": data}
    )
    assert result.ok, result.error


async def test_admin_group_target_asks_the_approver(tmp_path: Path) -> None:
    gate = RecordingGate(answer=True)
    async with harness(tmp_path, approval=gate) as h:
        await h.ha.set_state("group.locks", "locked", {"entity_id": ["lock.front"]})
        await wait_until(lambda: h.states.get("group.locks") is not None)
        agent = await h.agent(
            tools__ha={"level": "admin"}, approval={"entities": ["lock.front"]}
        )
        result = await h.call(
            agent,
            "ha.call_service",
            {
                "domain": "lock",
                "service": "unlock",
                "target": {"entity_id": "group.locks"},
            },
        )
        assert result.ok, result.error
        assert gate.requests == ["ha.call_service"]


async def test_state_cache_resync_permits_calls_again(h: Harness) -> None:
    agent = await h.agent(**READ_HALL)
    h.states._synced = False
    result = await h.call(agent, "ha.get_state", {"entity_id": "light.hall"})
    assert result.error_code == "states_unavailable"
    await h.ha.drop()
    await wait_until(lambda: not h.states.stale and not h.registry.stale)
    result = await h.call(agent, "ha.get_state", {"entity_id": "light.hall"})
    assert result.ok, result.error


async def test_check_failure_is_logged_and_audited(
    h: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    async def boom(rule_id: str) -> str:
        msg = "db locked"
        raise RuntimeError(msg)

    h.history.rule_owner = boom  # type: ignore[method-assign]
    agent = await h.agent()
    result = await h.call(agent, "memory.rule_stats", {"rule_id": "r1"})
    assert result.error_code == "check_failed"
    assert "db locked" not in (result.error or "")
    assert "db locked" in caplog.text
    [entry] = await h.audit.entries(agent.id)
    assert entry.decision == "denied"
    assert entry.reason_code == "check_failed"
