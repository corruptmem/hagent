"""The pure scope check: resolution, read checks and service calls."""

from __future__ import annotations

import json
from typing import Any

import pytest
from hagent_app.agents.definition import INDIRECT_DOMAINS, AgentDefinition, HAScope
from hagent_app.tools.registry import (
    RegistryUnavailableError,
    ServiceInfo,
    parse_service_info,
    parse_snapshot,
)
from hagent_app.tools.scope import (
    AgentScope,
    ScopeDeniedError,
    admin_entities,
    agent_scope,
    check_read,
    check_service_call,
    resolve_scope,
)
from helpers.agents import definition
from helpers.home import (
    DEV_BEDROOM,
    DEV_KITCHEN,
    DEV_THERMO,
    ENTITIES,
    SERVICES,
    snapshot,
)
from helpers.home import services as home_services

REG = snapshot()
KNOWN = frozenset(e["entity_id"] for e in ENTITIES)


def _scope(**changes: Any) -> AgentScope:
    data = definition(**changes)
    return agent_scope(AgentDefinition.from_json(json.dumps(data)), REG)


def _control(scope: dict[str, Any], services: list[str] | None = None) -> AgentScope:
    ha: dict[str, Any] = {"level": "control", "scope": scope}
    if services:
        ha["services"] = services
    return _scope(tools__ha=ha)


def _call(scope: AgentScope, name: str, **kwargs: Any) -> Any:
    domain, service = name.split(".")
    return check_service_call(scope, REG, KNOWN, domain, service, **kwargs)


def _denied(scope: AgentScope, name: str, **kwargs: Any) -> str:
    with pytest.raises(ScopeDeniedError) as info:
        _call(scope, name, **kwargs)
    return info.value.code


# ------------------------------------------------------------- resolution


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        ({"areas": ["kitchen"]}, {"light.kitchen", "switch.kettle"}),
        (
            {"areas": ["lounge"]},
            {
                "light.lounge",
                "media_player.lounge",
                "light.kitchen_spot",
                "climate.lounge",
                "sensor.lounge_temp",
            },
        ),
        ({"floors": ["upstairs"]}, {"light.bedroom"}),
        ({"labels": ["quiet"]}, {"light.bedroom"}),
        ({"labels": ["heating"]}, {"climate.lounge", "sensor.lounge_temp"}),
        ({"devices": [DEV_KITCHEN]}, {"light.kitchen", "light.kitchen_spot"}),
        ({"entities": ["lock.front"]}, {"lock.front"}),
        ({"areas": ["no_such_area"]}, set()),
    ],
)
def test_scope_resolves_registry_targets(
    scope: dict[str, Any], expected: set[str]
) -> None:
    resolved = resolve_scope(HAScope.model_validate_json(json.dumps(scope)), REG)
    assert set(resolved.entities) == expected


def test_scope_needs_registry_for_registry_targets() -> None:
    with pytest.raises(RegistryUnavailableError):
        resolve_scope(HAScope(floors=("ground",)), None)
    # Entities and domains need no registry.
    resolved = resolve_scope(
        HAScope(entities=("lock.front",), domains=("light",)), None
    )
    assert resolved.contains("lock.front")
    assert resolved.contains("light.anything")


def test_read_scope_includes_outputs_and_inputs() -> None:
    scope = _scope(
        tools__ha={"level": "read", "scope": {"entities": ["light.hall"]}},
        outputs=["input_boolean.guest"],
        inputs={"entities": ["lock.front"], "devices": [DEV_BEDROOM]},
    )
    check_read(scope, ["light.hall", "input_boolean.guest", "lock.front"])
    check_read(scope, ["light.bedroom"])
    with pytest.raises(ScopeDeniedError) as info:
        check_read(scope, ["light.kitchen"])
    assert info.value.code == "out_of_scope"


def test_read_needs_a_read_level() -> None:
    scope = _scope(outputs=["input_boolean.guest"])
    with pytest.raises(ScopeDeniedError) as info:
        check_read(scope, ["input_boolean.guest"])
    assert info.value.code == "level"


# ------------------------------------------------------- permitted calls


def test_area_target_becomes_explicit_entities() -> None:
    call = _call(
        _control({"areas": ["kitchen"]}), "light.turn_on", target={"area_id": "kitchen"}
    )
    # Only the light domain, and only the checked entities go to HA.
    assert call.target == {"entity_id": ["light.kitchen"]}
    assert call.path == "control"


def test_floor_target_resolves_to_entities() -> None:
    call = _call(
        _control({"floors": ["ground"]}), "light.turn_on", target={"floor_id": "ground"}
    )
    assert call.target == {
        "entity_id": ["light.kitchen", "light.kitchen_spot", "light.lounge"]
    }


def test_label_and_device_targets_resolve() -> None:
    scope = _control({"labels": ["heating"], "devices": [DEV_KITCHEN]})
    call = _call(scope, "climate.set_temperature", target={"label_id": "heating"})
    assert call.target == {"entity_id": ["climate.lounge"]}
    call = _call(scope, "light.turn_off", target={"device_id": DEV_KITCHEN})
    assert call.target == {"entity_id": ["light.kitchen", "light.kitchen_spot"]}


def test_target_keys_in_service_data_are_lifted() -> None:
    call = _call(
        _control({"entities": ["light.hall"]}),
        "light.turn_on",
        data={"entity_id": "light.hall", "brightness": 10},
    )
    assert call.target == {"entity_id": ["light.hall"]}
    assert call.data == {"brightness": 10}


def test_outputs_are_a_short_form_of_control() -> None:
    scope = _scope(outputs=["input_boolean.guest"])
    call = _call(
        scope, "input_boolean.turn_on", target={"entity_id": "input_boolean.guest"}
    )
    assert call.path == "outputs"


def test_scope_with_all_permits_the_all_token() -> None:
    call = _call(_control({"all": True}), "light.turn_off", target={"entity_id": "all"})
    assert call.target == {"entity_id": "all"}


def test_named_service_permits_a_call_without_target() -> None:
    scope = _control({"domains": ["light"]}, services=["recorder.purge", "light.*"])
    call = _call(scope, "recorder.purge", data={"keep_days": 5})
    assert call.target is None


def test_named_indirect_service_is_permitted() -> None:
    scope = _control({"entities": ["script.morning"]}, services=["script.turn_on"])
    call = _call(scope, "script.turn_on", target={"entity_id": "script.morning"})
    assert call.entities == {"script.morning"}


def test_admin_passes_unchanged() -> None:
    scope = _scope(tools__ha={"level": "admin"})
    call = _call(scope, "homeassistant.restart")
    assert call.path == "admin"
    # Admin needs no registry and no known service.
    call = check_service_call(scope, None, KNOWN, "any_domain", "any_service")
    assert call.path == "admin"


# ---------------------------------------------------------- denied calls

KITCHEN = {"areas": ["kitchen"]}
LOUNGE = {"areas": ["lounge"]}


@pytest.mark.parametrize(
    ("scope", "name", "kwargs", "code"),
    [
        # Targets outside the scope.
        (
            KITCHEN,
            "light.turn_on",
            {"target": {"entity_id": "light.hall"}},
            "out_of_scope",
        ),
        (KITCHEN, "light.turn_on", {"target": {"area_id": "lounge"}}, "out_of_scope"),
        (KITCHEN, "light.turn_on", {"target": {"floor_id": "ground"}}, "out_of_scope"),
        (KITCHEN, "light.turn_on", {"target": {"label_id": "quiet"}}, "out_of_scope"),
        (
            KITCHEN,
            "light.turn_on",
            {"target": {"device_id": DEV_BEDROOM}},
            "out_of_scope",
        ),
        (
            KITCHEN,
            "light.turn_on",
            {"target": {"entity_id": ["light.kitchen", "light.hall"]}},
            "out_of_scope",
        ),
        (
            KITCHEN,
            "light.turn_on",
            {"data": {"entity_id": "light.hall"}},
            "out_of_scope",
        ),
        # Entities inside the service data.
        (
            LOUNGE,
            "media_player.join",
            {
                "target": {"entity_id": "media_player.lounge"},
                "data": {"group_members": ["media_player.other"]},
            },
            "out_of_scope",
        ),
        (
            LOUNGE,
            "media_player.join",
            {
                "target": {"entity_id": "media_player.lounge"},
                "data": {"group_members": "MEDIA_PLAYER.OTHER"},
            },
            "out_of_scope",
        ),
        (
            LOUNGE,
            "media_player.join",
            {
                "target": {"entity_id": "media_player.lounge"},
                "data": {"group_members": "media_player.lounge, media_player.other"},
            },
            "out_of_scope",
        ),
        (
            KITCHEN,
            "light.turn_on",
            {
                "target": {"entity_id": "light.kitchen"},
                "data": {"x": {"entity_id": "lock.front"}},
            },
            "out_of_scope",
        ),
        (
            KITCHEN,
            "light.turn_on",
            {"target": {"entity_id": "light.kitchen"}, "data": {"x": [DEV_BEDROOM]}},
            "out_of_scope",
        ),
        (
            KITCHEN,
            "light.turn_on",
            {
                "target": {"entity_id": "light.kitchen"},
                "data": {"x": {"area_id": "lounge"}},
            },
            "invalid_target",
        ),
        # Templates, unknown keys, unknown targets, bad IDs.
        (
            KITCHEN,
            "light.turn_on",
            {
                "target": {"entity_id": "light.kitchen"},
                "data": {"effect": "{{ states('lock.front') }}"},
            },
            "template",
        ),
        (
            KITCHEN,
            "light.turn_on",
            {"target": {"entity_id": "light.kitchen"}, "data": {"x": "{% if 1 %}"}},
            "template",
        ),
        (KITCHEN, "light.turn_on", {"target": {"zone_id": "home"}}, "invalid_target"),
        (KITCHEN, "light.turn_on", {"target": {"area_id": "attic"}}, "invalid_target"),
        (
            KITCHEN,
            "light.turn_on",
            {"target": {"floor_id": "cellar"}},
            "invalid_target",
        ),
        (KITCHEN, "light.turn_on", {"target": {"label_id": "x"}}, "invalid_target"),
        (
            KITCHEN,
            "light.turn_on",
            {"target": {"device_id": "f" * 32}},
            "invalid_target",
        ),
        (
            KITCHEN,
            "light.turn_on",
            {"target": {"entity_id": "Light.Kitchen"}},
            "invalid_target",
        ),
        (KITCHEN, "light.turn_on", {"target": {"entity_id": 5}}, "invalid_target"),
        (KITCHEN, "light.turn_on", {"target": {"entity_id": "all"}}, "out_of_scope"),
        (KITCHEN, "light.turn_on", {"target": {"entity_id": "none"}}, "out_of_scope"),
        (
            KITCHEN,
            "light.fly",
            {"target": {"entity_id": "light.kitchen"}},
            "unknown_service",
        ),
        # Services with no target in the scope. mqtt and recorder are
        # indirect domains, so the indirect rule stops them first.
        (
            {"all": True},
            "mqtt.publish",
            {"data": {"topic": "x", "payload": "y"}},
            "indirect_service",
        ),
        ({"all": True}, "recorder.purge", {}, "indirect_service"),
        (
            {"all": True},
            "conversation.process",
            {"data": {"text": "unlock the front door"}},
            "no_target",
        ),
        (KITCHEN, "light.turn_on", {}, "no_target"),
        # Indirect services named by a wildcard domain scope.
        (
            {"domains": ["script"]},
            "script.turn_on",
            {"target": {"entity_id": "script.morning"}},
            "indirect_service",
        ),
        (
            {"all": True},
            "homeassistant.turn_on",
            {"target": {"entity_id": "light.kitchen"}},
            "indirect_service",
        ),
    ],
)
def test_out_of_scope_calls_are_denied(
    scope: dict[str, Any], name: str, kwargs: dict[str, Any], code: str
) -> None:
    assert _denied(_control(scope), name, **kwargs) == code


@pytest.mark.parametrize("domain", sorted(INDIRECT_DOMAINS))
def test_every_indirect_service_is_denied_unless_named(domain: str) -> None:
    service, description = next(iter(SERVICES[domain].items()))
    # Send a target only if the service declares one.
    target = {"entity_id": f"{domain}.x"} if "target" in description else None
    assert _denied(_control({"all": True}), f"{domain}.{service}", target=target) == (
        "indirect_service"
    )
    # A list of other services does not permit it either.
    scope = _control({"all": True}, services=["light.turn_on"])
    assert _denied(scope, f"{domain}.{service}", target=target) in {
        "indirect_service",
        "service_not_permitted",
    }
    # The exact name permits it.
    scope = _control({"all": True}, services=[f"{domain}.{service}"])
    assert _call(scope, f"{domain}.{service}", target=target).path == "control"


def test_group_members_must_be_in_the_scope() -> None:
    scope = _control({"entities": ["light.group", "light.kitchen"]})
    members = {"light.group": ["light.kitchen", "light.lounge"]}
    target = {"entity_id": "light.group"}
    with pytest.raises(ScopeDeniedError) as info:
        _call(scope, "light.turn_on", target=target, group_members=members)
    assert info.value.code == "out_of_scope"
    members = {"light.group": ["light.kitchen"]}
    call = _call(scope, "light.turn_on", target=target, group_members=members)
    assert call.data_entities == {"light.kitchen"}


def test_nested_groups_are_expanded_and_limited() -> None:
    scope = _control({"entities": ["light.g0", "light.g1", "light.kitchen"]})
    members = {"light.g0": ["light.g1"], "light.g1": ["light.hall"]}
    with pytest.raises(ScopeDeniedError) as info:
        _call(
            scope,
            "light.turn_on",
            target={"entity_id": "light.g0"},
            group_members=members,
        )
    assert info.value.code == "out_of_scope"
    deep = {f"light.g{i}": [f"light.g{i + 1}"] for i in range(10)}
    with pytest.raises(ScopeDeniedError) as info:
        _call(
            _control({"all": True}),
            "light.turn_on",
            target={"entity_id": "light.g0"},
            group_members=deep,
        )
    assert info.value.code == "invalid_target"


def test_entity_ids_in_data_keys_are_checked() -> None:
    scope = _control({"entities": ["light.kitchen"]}, services=["scene.apply"])
    data: dict[str, Any] = {"entities": {"lock.front": {"state": "unlocked"}}}
    assert _denied(scope, "scene.apply", data=data) == "out_of_scope"
    data = {"entities": {"light.kitchen": {"state": "on"}}}
    assert _call(scope, "scene.apply", data=data).path == "control"
    data = {"entities": {"{{ 1 }}": "on"}}
    assert _denied(scope, "scene.apply", data=data) == "template"


def test_registry_target_without_domain_entities_is_denied() -> None:
    scope = _control({"all": True}, services=["light.turn_on"])
    code = _denied(scope, "light.turn_on", target={"device_id": DEV_THERMO})
    assert code == "invalid_target"


def test_services_list_limits_the_services() -> None:
    scope = _control(KITCHEN, services=["light.turn_off"])
    code = _denied(scope, "light.turn_on", target={"entity_id": "light.kitchen"})
    assert code == "service_not_permitted"


def test_outputs_do_not_permit_indirect_services_or_other_entities() -> None:
    scope = _scope(outputs=["script.morning", "input_boolean.guest"])
    assert (
        _denied(scope, "script.turn_on", target={"entity_id": "script.morning"})
        == "indirect_service"
    )
    target = {"entity_id": ["input_boolean.guest", "light.hall"]}
    assert _denied(scope, "input_boolean.turn_on", target=target) == "out_of_scope"


def test_read_level_cannot_call_services() -> None:
    scope = _scope(tools__ha={"level": "read", "scope": {"all": True}})
    assert (
        _denied(scope, "light.turn_on", target={"entity_id": "light.kitchen"})
        == "level"
    )


def test_stale_registry_denies_control() -> None:
    scope = _control({"entities": ["light.hall"]})
    with pytest.raises(RegistryUnavailableError):
        check_service_call(
            scope, None, KNOWN, "light", "turn_on", target={"entity_id": "light.hall"}
        )


def test_denial_does_not_name_hidden_entities() -> None:
    with pytest.raises(ScopeDeniedError) as info:
        _call(_control(KITCHEN), "light.turn_on", target={"area_id": "lounge"})
    assert "light.lounge" not in info.value.message
    assert "2 entities" in info.value.message


# ------------------------------------------- review-8 regressions (pure)


def test_cross_domain_service_needs_the_exact_name() -> None:
    # remote_bridge.send declares an entity target with no domain filter.
    target = {"entity_id": "light.hall"}
    data = {"command": "x"}
    scope = _control({"all": True})
    assert _denied(scope, "remote_bridge.send", target=target, data=data) == (
        "cross_domain"
    )
    wildcard = _control({"all": True}, services=["remote_bridge.*"])
    assert _denied(wildcard, "remote_bridge.send", target=target, data=data) == (
        "cross_domain"
    )
    named = _control({"all": True}, services=["remote_bridge.send"])
    assert _call(named, "remote_bridge.send", target=target, data=data).path == (
        "control"
    )


def test_outputs_never_permit_a_cross_domain_service() -> None:
    scope = _scope(outputs=["light.hall"])
    target = {"entity_id": "light.hall"}
    assert _denied(scope, "remote_bridge.send", target=target) == "cross_domain"


def test_recorder_purge_entities_cannot_purge_everything() -> None:
    target = {"entity_id": "light.hall"}
    data = {"entity_globs": ["*"], "domains": ["lock", "sensor"], "keep_days": 0}
    # Control on one light: recorder is indirect.
    scope = _control({"entities": ["light.hall"]})
    assert _denied(scope, "recorder.purge_entities", target=target, data=data) == (
        "indirect_service"
    )
    # Named exactly: the service declares no entity target.
    named = _control({"entities": ["light.hall"]}, services=["recorder.purge_entities"])
    assert _denied(named, "recorder.purge_entities", target=target, data=data) == (
        "target_not_declared"
    )
    # Only an output.
    outputs = _scope(outputs=["input_boolean.guest"])
    assert (
        _denied(
            outputs,
            "recorder.purge_entities",
            target={"entity_id": "input_boolean.guest"},
            data={"entity_globs": ["*"]},
        )
        == "indirect_service"
    )


def test_zwave_broadcast_through_an_output_is_denied() -> None:
    data = {
        "broadcast": True,
        "command_class": 37,
        "property": "targetValue",
        "value": True,
    }
    scope = _scope(outputs=["light.hall"])
    code = _denied(
        scope,
        "zwave_js.multicast_set_value",
        target={"entity_id": "light.hall"},
        data=data,
    )
    assert code == "indirect_service"


def test_undeclared_data_keys_are_denied() -> None:
    scope = _control(KITCHEN)
    target = {"entity_id": "light.kitchen"}
    assert _denied(
        scope, "light.turn_on", target=target, data={"domains": ["lock"]}
    ) == ("unknown_field")
    # Fields in a section are declared fields.
    call = _call(scope, "light.turn_on", target=target, data={"flash": "short"})
    assert call.data == {"flash": "short"}


def test_service_without_metadata_is_denied() -> None:
    services = {d: dict(v) for d, v in home_services().items()}
    services["light"]["turn_on"] = {"name": "no fields key"}
    reg = parse_snapshot(
        entities=[{"entity_id": e["entity_id"]} for e in ENTITIES],
        devices=[],
        areas=[],
        floors=[],
        labels=[],
        services=services,
    )
    scope = agent_scope(
        AgentDefinition.from_json(
            json.dumps(
                definition(
                    tools__ha={
                        "level": "control",
                        "scope": {"entities": ["light.hall"]},
                    }
                )
            )
        ),
        reg,
    )
    with pytest.raises(ScopeDeniedError) as info:
        check_service_call(
            scope, reg, KNOWN, "light", "turn_on", target={"entity_id": "light.hall"}
        )
    assert info.value.code == "service_metadata"


def test_target_must_be_declared_for_the_entity_domain() -> None:
    scope = _control({"domains": ["scene"]}, services=["scene.apply"])
    code = _denied(scope, "scene.apply", target={"entity_id": "scene.x"})
    assert code == "target_not_declared"


@pytest.mark.parametrize(
    ("service", "data"),
    [
        ("group.set", {"object_id": "everyone", "entities": ["light.hall"]}),
        ("group.remove", {"object_id": "everyone"}),
    ],
)
def test_group_object_id_must_be_in_the_scope(service: str, data: Any) -> None:
    scope = _control({"entities": ["light.hall"]}, services=[service])
    with pytest.raises(ScopeDeniedError) as info:
        _call(scope, service, data=data)
    assert info.value.code == "out_of_scope"
    assert "group.everyone" in info.value.message
    allowed = _control(
        {"entities": ["light.hall", "group.everyone"]}, services=[service]
    )
    call = _call(allowed, service, data=data)
    assert "group.everyone" in call.data_entities
    assert call.data["object_id"] == "everyone"


def test_group_object_id_must_be_a_slug() -> None:
    scope = _control({"all": True}, services=["group.remove"])
    assert _denied(scope, "group.remove", data={"object_id": "a.b"}) == "invalid_data"
    assert _denied(scope, "group.remove", data={"object_id": 5}) == "invalid_data"


def test_entity_id_with_a_trailing_newline_is_denied() -> None:
    scope = _control(KITCHEN)
    code = _denied(scope, "light.turn_on", target={"entity_id": "light.kitchen\n"})
    assert code == "invalid_target"
    with pytest.raises(ScopeDeniedError):
        check_read(scope, ["light.kitchen\n"])
    with pytest.raises(ScopeDeniedError):
        _call(scope, "light.turn_on\n", target={"entity_id": "light.kitchen"})


def test_admin_entities_expand_groups_and_scan_data() -> None:
    members = {"group.locks": ("lock.front",)}
    found = admin_entities(REG, KNOWN, {}, {"entity_id": "group.locks"}, members)
    assert found.entities == {"group.locks", "lock.front"}
    assert found.complete
    found = admin_entities(
        REG, KNOWN, {"entities": {"lock.front": {"state": "unlocked"}}}, None
    )
    assert "lock.front" in found.entities
    assert found.complete


def test_admin_entities_report_an_incomplete_scan() -> None:
    template = {"entities": {"{{ 'lock.front' }}": "unlocked"}}
    assert not admin_entities(REG, KNOWN, template, None).complete
    assert not admin_entities(None, KNOWN, {}, {"entity_id": "lock.front"}).complete
    deep = {f"group.g{i}": (f"group.g{i + 1}",) for i in range(10)}
    assert not admin_entities(REG, KNOWN, {}, {"entity_id": "group.g0"}, deep).complete


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        (
            {"fields": {}, "target": {"entity": {"domain": "light"}}},
            ServiceInfo(frozenset({"light"}), False, frozenset()),
        ),
        (
            {"fields": {}, "target": {"entity": [{"domain": ["light", "switch"]}]}},
            ServiceInfo(frozenset({"light", "switch"}), False, frozenset()),
        ),
        (
            {"fields": {}, "target": {"entity": [{"integration": "x"}]}},
            ServiceInfo(frozenset(), True, frozenset()),
        ),
        (
            {"fields": {}, "target": {"device": {}}},
            ServiceInfo(None, False, frozenset()),
        ),
        (
            {"fields": {}, "target": {"entity": [{"domain": 5}]}},
            ServiceInfo(None, False, frozenset()),
        ),
        (
            {
                "fields": {
                    "a": {"selector": {"text": None}},
                    "sec": {"collapsed": True, "fields": {"b": {"selector": {}}}},
                }
            },
            ServiceInfo(None, False, frozenset({"a", "b"})),
        ),
        ({}, None),
        ({"fields": []}, None),
        ("x", None),
    ],
)
def test_service_descriptions_are_parsed(
    description: Any, expected: ServiceInfo | None
) -> None:
    assert parse_service_info(description) == expected
