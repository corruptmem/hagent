"""Tests of the rule template sandbox (task-13, design section 10.2)."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from hagent_app.ha import State
from hagent_app.rules.templates import (
    MAX_TEMPLATE,
    ScopedStates,
    TemplateClock,
    TemplateEngine,
    TemplateError,
    check_template,
    is_true,
    parse_result,
)

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def _state(entity_id: str, state: str, **attributes: Any) -> State:
    return State(entity_id, state, attributes, NOW, NOW, {"user_id": "u1"})


STATES = [
    _state("sensor.temp", "21.5", unit_of_measurement="°C", friendly_name="Temp"),
    _state("sensor.hidden", "secret", note="private"),
    _state("binary_sensor.door", "on"),
    _state("sensor.gone", "unavailable"),
]


def engine(visible: set[str] | None = None, seconds: float = 1.0) -> TemplateEngine:
    allowed = visible if visible is not None else {"sensor.temp", "binary_sensor.door"}
    allowed |= {"sensor.gone"} if visible is None else set()
    view = ScopedStates.build(STATES, allowed.__contains__)
    return TemplateEngine(view, TemplateClock(ZoneInfo("Europe/London")), seconds)


async def render(source: str, **variables: Any) -> Any:
    return await engine().render_value(source, variables)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("{{ states('sensor.temp') }}", 21.5),
        ("{{ states('sensor.temp', with_unit=True) }}", "21.5 °C"),
        ("{{ states('sensor.hidden') }}", "unknown"),
        ("{{ states('sensor.nope') }}", "unknown"),
        ("{{ states.sensor.hidden }}", None),
        ("{{ states.sensor.hidden is none }}", True),
        (
            "{{ states.sensor | map(attribute='entity_id') | list }}",
            ["sensor.gone", "sensor.temp"],
        ),
        ("{{ states | count }}", 3),
        ("{{ is_state('sensor.hidden', 'secret') }}", False),
        ("{{ is_state('binary_sensor.door', ['on', 'off']) }}", True),
        ("{{ state_attr('sensor.hidden', 'note') }}", None),
        ("{{ state_attr('sensor.temp', 'friendly_name') }}", "Temp"),
        ("{{ is_state_attr('sensor.temp', 'friendly_name', 'Temp') }}", True),
        ("{{ has_value('sensor.gone') }}", False),
        ("{{ has_value('sensor.hidden') }}", False),
        ("{{ 'sensor.temp' | has_value }}", True),
        ("{{ 'binary_sensor.door' is is_state('on') }}", True),
        ("{{ states.sensor.temp.name }}", "Temp"),
        ("{{ states.sensor.temp.last_changed.year }}", 2026),
    ],
)
async def test_state_functions_read_only_the_scoped_view(
    source: str, expected: Any
) -> None:
    assert await render(source) == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("{{ 'x' | float(0) }}", 0),
        ("{{ '4' | int + 1 }}", 5),
        ("{{ 'on' | bool }}", True),
        ("{{ 2.567 | round(1) }}", 2.6),
        ("{{ 2.4 | round(0, 'ceil') }}", 3.0),
        ("{{ iif(true, 'a', 'b') }}", "a"),
        ("{{ none | iif('a', 'b', 'c') }}", "c"),
        ("{{ average(1, 2, 3) }}", 2.0),
        ("{{ [1, 5, 3] | median }}", 3),
        ("{{ max(1, 7, 3) }}", 7),
        ("{{ min([4, 2]) }}", 2),
        ("{{ '12' is is_number }}", True),
        # HA also parses the rendered JSON text into a value.
        ("{{ {'a': 1} | to_json }}", {"a": 1}),
        ("{{ {'a': 1} | to_json }}x", '{"a":1}x'),
        ("{{ ('{\"a\": [1]}' | from_json).a[0] }}", 1),
        ("{{ 'abc' | regex_match('a') }}", True),
        ("{{ 'abc' | regex_replace('b', 'x') }}", "axc"),
        ("{{ 'a1b22' | regex_findall('[0-9]+') }}", ["1", "22"]),
        ("{{ (timedelta(hours=2)).total_seconds() }}", 7200.0),
        ("{{ 3 | multiply(2) }}", 6.0),
        ("{{ [1, 2] | join(',') }}", (1, 2)),
        ("{{ ['a', 'b'] | join(', ') }}", "a, b"),
        ("{{ '%.1f' | format(2.25) }}", 2.2),
        ("{{ 0 | timestamp_utc }}", "1970-01-01T00:00:00+00:00"),
        ("{{ as_timestamp('1970-01-01T00:00:10+00:00') }}", 10.0),
        ("{{ inputs['sensor.temp'] }}", "x"),
    ],
)
async def test_ha_functions_of_the_subset(source: str, expected: Any) -> None:
    assert await render(source, inputs={"sensor.temp": "x"}) == expected


async def test_now_uses_the_ha_time_zone() -> None:
    assert await render("{{ now().tzinfo }}") == "Europe/London"
    assert await render("{{ utcnow().tzinfo }}") == "UTC"
    value = await render("{{ (now() - timedelta(minutes=1)) < now() }}")
    assert value is True
    assert isinstance(TemplateClock().now().utcoffset(), timedelta)


async def test_float_without_a_default_fails_as_in_ha() -> None:
    with pytest.raises(TemplateError, match="no default"):
        await render("{{ 'abc' | float }}")


# ------------------------------------------------------------- the sandbox


@pytest.mark.parametrize(
    "source",
    [
        "{{ states.sensor.temp.context }}",
        "{{ ''.__class__ }}",
        "{{ states.__init__ }}",
        "{{ states.sensor.temp._view }}",
    ],
)
async def test_internals_are_not_readable(source: str) -> None:
    assert await render(source) in ("", None)


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("{{ 'a'.ljust(100000000) }}", "not safely callable"),
        ("{{ '{}'.format(1) }}", "not safely callable"),
        ("{{ 'x' * 10000000 }}", "repeated value"),
        ("{{ [1] * 10000000 }}", "repeated value"),
        ("{{ 2 ** 100000 }}", "power is too large"),
        ("{{ '%099999d' % 1 }}", "format width"),
        ("{{ '%099999d' | format(1) }}", "format width"),
        ("{{ (['x' * 200000] * 2) | join }}", "longer than"),
        ("{{ ('x' * 200000) | replace('x', 'yy') }}", "longer than"),
        ("{% set l = [] %}{{ l.append(1) }}", "unsafe"),
    ],
)
async def test_large_or_unsafe_operations_fail(source: str, message: str) -> None:
    with pytest.raises(TemplateError, match=message):
        await render(source)


async def test_a_render_stops_at_the_time_limit() -> None:
    slow = (
        "{% for i in range(100000) %}{% for j in range(100000) %}"
        "{% endfor %}{% endfor %}"
    )
    with pytest.raises(TemplateError, match="took longer"):
        await engine(seconds=0.2).render(slow, {})


async def test_the_output_has_a_size_limit() -> None:
    big = "{% for i in range(3) %}{{ 'x' * 100000 }}{% endfor %}"
    with pytest.raises(TemplateError, match="output is longer"):
        await render(big)


async def test_center_and_indent_widths_are_capped() -> None:
    assert await render("{{ 'a' | center(100000000) | length }}") == 1000
    assert await render("{{ 'a\nb' | indent(100000000) | length }}") == 1003


# -------------------------------------------------------- the static check


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("{{ area_entities('kitchen') }}", "unknown names"),
        ("{{ lipsum() }}", "unknown names"),
        ("{{ x | xmlattr }}", "filter 'xmlattr'"),
        ("{{ 1 | pprint }}", "filter 'pprint'"),
        ("{{ 1 is callable }}", "test 'callable'"),
        ("{% import 'x' as y %}", "import"),
        ("{% include 'x' %}", "include"),
        ("{% extends 'x' %}", "extends"),
        ("{% macro m() %}{% endmacro %}", "macro"),
        ("{{ 1 +", "syntax error"),
        ("x" * (MAX_TEMPLATE + 1), "longer than"),
    ],
)
def test_the_static_check_rejects_templates_outside_the_subset(
    source: str, message: str
) -> None:
    with pytest.raises(TemplateError, match=message):
        check_template(source)


def test_the_static_check_accepts_the_rule_variables() -> None:
    check_template(
        "{% set n = namespace(x=0) %}{{ source }}{{ inputs }}{{ data }}"
        "{{ context }}{{ output }}{{ answers }}{{ result }}"
        "{{ states('a.b') | float(0) | round(1) }}{{ now() }}{{ n.x }}"
    )


# --------------------------------------------------------- result parsing


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("5", 5),
        ("1.50", 1.5),
        ("01", "01"),
        ("True", True),
        ("[1, 'a']", [1, "a"]),
        ("{'a': 1}", {"a": 1}),
        ("None", None),
        ("hello", "hello"),
        ("'quoted'", "'quoted'"),
        ("1e3", "1e3"),
        ("{1, 2}", "{1, 2}"),
    ],
)
def test_parse_result_follows_ha(text: str, expected: Any) -> None:
    assert parse_result(text) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        (1, True),
        (0, False),
        ("on", True),
        ("False", False),
        (None, False),
    ],
)
def test_is_true(value: Any, expected: bool) -> None:
    assert is_true(value) is expected


# ------------------------------------------- size limits cannot be bypassed

S = "{% set s = 'x' * 200000 %}"
NESTED = (
    "{% set l = range(1000) | list %}{% set a = [l, l, l, l] %}"
    "{% set b = [a, a, a, a] %}{% set c = [b, b, b, b] %}"
    "{% set d = [c, c, c, c] %}"
)


@pytest.mark.parametrize(
    "source",
    [
        S + "{{ (s + s) | length }}",
        S + "{% set t = s ~ s %}{{ t | length }}",
        S + "{{ s ~ s }}",
        NESTED + "{{ d | string | length }}",
        NESTED + "{{ d }}",
        NESTED + "{{ d | to_json | length }}",
        NESTED + "{{ d | tojson | length }}",
        NESTED + "{{ d | lower | length }}",
        NESTED + "{{ [d] | join | length }}",
        NESTED + "{{ '%s' % (d,) }}",
        NESTED + "{{ d | regex_replace('x', 'y') | length }}",
        "{{ [1] | batch(300000, 0) | first | length }}",
        "{{ ([[0] * 1000] * 1000) | sum(start=[]) | length }}",
        S + "{{ s | regex_replace('x', 'yy') | length }}",
        S + "{{ ('a\\n' * 100000) | indent(s) | length }}",
    ],
)
async def test_size_limits_hold_for_every_path(source: str) -> None:
    with pytest.raises(TemplateError):
        await engine(seconds=5).render(source, {})


async def test_a_stuck_render_ends_the_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """One slow call in C cannot be stopped; the outer time limit ends the
    wait, so the invocation ends."""
    release = threading.Event()

    def stuck(self: TemplateEngine, source: str, variables: Any) -> str:
        release.wait(5)
        return ""

    monkeypatch.setattr(TemplateEngine, "_render_sync", stuck)
    try:
        started = time.monotonic()
        with pytest.raises(TemplateError, match="took longer"):
            await engine(seconds=0.1).render("x", {})
        assert time.monotonic() - started < 3
    finally:
        release.set()
