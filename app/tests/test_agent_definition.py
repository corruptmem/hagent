"""Validation of every agent definition field (design section 7.1)."""

import json
from typing import Any

import pytest
from hagent_app.agents.definition import AgentDefinition, default_definition_data
from helpers.agents import definition
from pydantic import ValidationError

STORE = "a" * 32


def _parse(data: dict[str, Any]) -> AgentDefinition:
    return AgentDefinition.from_json(json.dumps(data))


def test_defaults_match_the_design() -> None:
    d = _parse(definition())
    assert d.models.summary_model == d.models.chat
    assert d.models.decision is None
    assert d.result.type == "text"
    assert d.tools.ha.level == "none"
    assert d.retrieval.mode == "off"
    assert d.retrieval.result_count == 5
    assert d.retrieval.token_limit == 1000
    assert d.assist.enabled is False
    assert d.budget.daily_usd == 0.50
    assert d.budget.max_tool_calls == 20
    assert d.concurrency.mode == "queued"
    assert d.concurrency.queue_size == 10
    assert d.fallback.mode == "last_result"
    assert d.scheduling.min_interval_seconds == 60
    assert d.scheduling.max_schedules == 20
    assert d.review.enabled is True


def test_default_data_round_trips() -> None:
    data = default_definition_data()
    assert _parse(data).to_data() == data


VALID: list[tuple[str, dict[str, Any]]] = [
    (
        "summary model",
        definition(models__summary={"provider": "openrouter", "model": "x/y"}),
    ),
    (
        "decision model",
        definition(models__decision={"provider": "openai", "model": "m"}),
    ),
    ("inputs", definition(inputs={"entities": ["sensor.t"], "devices": ["abc123"]})),
    (
        "structured result",
        definition(result={"type": "structured", "schema": {"type": "object"}}),
    ),
    ("none result", definition(result={"type": "none"})),
    ("ha read all", definition(tools__ha={"level": "read", "scope": {"all": True}})),
    (
        "named indirect service",
        definition(
            tools__ha={
                "level": "control",
                "scope": {"areas": ["kitchen"]},
                "services": ["script.turn_on", "light.*"],
            }
        ),
    ),
    ("ha admin", definition(tools__ha={"level": "admin"})),
    ("knowledge", definition(knowledge=[{"store_id": STORE, "access": "read_write"}])),
    ("parallel without outputs", definition(concurrency__mode="parallel")),
    ("fixed fallback", definition(fallback={"mode": "fixed", "value": {"t": 20}})),
    ("approval of a tool", definition(tools__self=True, approval__tools=["self.*"])),
    (
        "approval of output",
        definition(outputs=["climate.x"], approval__entities=["climate.x"]),
    ),
    ("budget float", definition(budget__daily_usd=2)),
    ("review cron", definition(review__cron="*/30 6-22 * * 1-5")),
]


@pytest.mark.parametrize(("case", "data"), VALID, ids=[c for c, _ in VALID])
def test_valid_definitions(case: str, data: dict[str, Any]) -> None:
    assert _parse(data).to_data()["name"] == "Heating", case


# One case or more for each row of section 7.1, and one for each rule
# between fields. Each case names the field path that must fail.
INVALID: list[tuple[str, dict[str, Any], str]] = [
    # Name and instructions
    ("empty name", definition(name="  "), "name"),
    ("long name", definition(name="x" * 65), "name"),
    ("empty instructions", definition(instructions=""), "instructions"),
    ("unknown field", definition(colour="red"), "colour"),
    # Models
    ("no chat model", definition(models={}), "models.chat"),
    (
        "bad provider",
        definition(models__chat={"provider": "acme", "model": "m"}),
        "models.chat.provider",
    ),
    (
        "empty model",
        definition(models__chat={"provider": "openai", "model": " "}),
        "models.chat.model",
    ),
    # Inputs
    (
        "bad input entity",
        definition(inputs__entities=["Light.Kitchen"]),
        "inputs.entities.0",
    ),
    ("duplicate input", definition(inputs__entities=["a.b", "a.b"]), "inputs.entities"),
    ("bad device id", definition(inputs__devices=["x y"]), "inputs.devices.0"),
    # Outputs
    ("bad output", definition(outputs=["kitchen"]), "outputs.0"),
    ("output as string", definition(outputs="light.a"), "outputs"),
    # Result type
    ("unknown result type", definition(result={"type": "json"}), "result.type"),
    ("structured without schema", definition(result={"type": "structured"}), "result"),
    (
        "schema not object",
        definition(result={"type": "structured", "schema": {"type": "array"}}),
        "result",
    ),
    (
        "text with schema",
        definition(result={"type": "text", "schema": {"type": "object"}}),
        "result",
    ),
    # Tools and scopes
    ("unknown ha level", definition(tools__ha={"level": "write"}), "tools.ha.level"),
    ("read with empty scope", definition(tools__ha={"level": "read"}), "tools.ha"),
    (
        "admin with scope",
        definition(tools__ha={"level": "admin", "scope": {"all": True}}),
        "tools.ha",
    ),
    (
        "none with services",
        definition(tools__ha={"level": "none", "services": ["light.turn_on"]}),
        "tools.ha",
    ),
    (
        "read with services",
        definition(
            tools__ha={
                "level": "read",
                "scope": {"all": True},
                "services": ["light.turn_on"],
            }
        ),
        "tools.ha",
    ),
    (
        "indirect wildcard",
        definition(
            tools__ha={
                "level": "control",
                "scope": {"all": True},
                "services": ["script.*"],
            }
        ),
        "tools.ha",
    ),
    (
        "notify wildcard",
        definition(
            tools__ha={
                "level": "control",
                "scope": {"all": True},
                "services": ["notify.*"],
            }
        ),
        "tools.ha",
    ),
    (
        "bad service",
        definition(
            tools__ha={
                "level": "control",
                "scope": {"all": True},
                "services": ["turn_on"],
            }
        ),
        "tools.ha.services.0",
    ),
    (
        "all plus list",
        definition(tools__ha={"level": "read", "scope": {"all": True, "areas": ["x"]}}),
        "tools.ha.scope",
    ),
    (
        "bad domain",
        definition(tools__ha={"level": "read", "scope": {"domains": ["Light"]}}),
        "tools.ha.scope.domains.0",
    ),
    ("tool group not bool", definition(tools__memory="yes"), "tools.memory"),
    # Knowledge stores
    ("bad store id", definition(knowledge=[{"store_id": "x"}]), "knowledge.0.store_id"),
    (
        "bad access",
        definition(knowledge=[{"store_id": STORE, "access": "write"}]),
        "knowledge.0.access",
    ),
    (
        "store twice",
        definition(knowledge=[{"store_id": STORE}, {"store_id": STORE}]),
        "",
    ),
    # Retrieval
    ("bad recall mode", definition(retrieval__mode="sometimes"), "retrieval.mode"),
    ("zero results", definition(retrieval__result_count=0), "retrieval.result_count"),
    ("low token limit", definition(retrieval__token_limit=50), "retrieval.token_limit"),
    # Approval
    ("unknown tool group", definition(approval__tools=["web.search"]), ""),
    ("approval of an off group", definition(approval__tools=["self"]), ""),
    ("approval of ha when off", definition(approval__tools=["ha.call_service"]), ""),
    (
        "service approval without control",
        definition(approval__services=["light.turn_on"]),
        "",
    ),
    ("bad tool reference", definition(approval__tools=["Self!"]), "approval.tools.0"),
    # Assist
    ("assist not bool", definition(assist__enabled="yes"), "assist.enabled"),
    # Budget
    ("negative budget", definition(budget__daily_usd=-1), "budget.daily_usd"),
    ("budget as text", definition(budget__daily_usd="0.5"), "budget.daily_usd"),
    ("zero tool calls", definition(budget__max_tool_calls=0), "budget.max_tool_calls"),
    ("few tokens", definition(budget__max_tokens=10), "budget.max_tokens"),
    # Concurrency
    ("unknown mode", definition(concurrency__mode="burst"), "concurrency.mode"),
    (
        "parallel with outputs",
        definition(concurrency__mode="parallel", outputs=["light.a"]),
        "",
    ),
    ("zero queue", definition(concurrency__queue_size=0), "concurrency.queue_size"),
    # Fallback
    ("unknown fallback", definition(fallback__mode="retry"), "fallback.mode"),
    ("fixed without value", definition(fallback={"mode": "fixed"}), "fallback"),
    (
        "error with value",
        definition(fallback={"mode": "error", "value": 1}),
        "fallback",
    ),
    # Scheduling limits
    (
        "zero interval",
        definition(scheduling__min_interval_seconds=0),
        "scheduling.min_interval_seconds",
    ),
    (
        "negative schedules",
        definition(scheduling__max_schedules=-1),
        "scheduling.max_schedules",
    ),
    (
        "negative debounce",
        definition(scheduling__debounce_seconds=-1),
        "scheduling.debounce_seconds",
    ),
    # Review schedule
    ("review word", definition(review__cron="daily"), "review.cron"),
    ("short cron", definition(review__cron="* * *"), "review.cron"),
]


@pytest.mark.parametrize(
    ("case", "data", "loc"), INVALID, ids=[c for c, _, _ in INVALID]
)
def test_invalid_definitions(case: str, data: dict[str, Any], loc: str) -> None:
    with pytest.raises(ValidationError) as info:
        _parse(data)
    locs = [".".join(str(p) for p in e["loc"]) for e in info.value.errors()]
    assert loc in locs, (case, locs)


def test_definition_is_frozen() -> None:
    d = _parse(definition(outputs=["light.a"]))
    with pytest.raises(ValidationError):
        d.name = "Other"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        d.budget.daily_usd = 99  # type: ignore[misc]
    assert isinstance(d.outputs, tuple)
    assert isinstance(d.inputs.entities, tuple)
    assert isinstance(d.knowledge, tuple)
