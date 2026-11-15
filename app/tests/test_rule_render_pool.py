"""Regression tests of the task-13 security review (rule templates).

Each test comes from a review probe. The in-process tests call
``render_sync`` directly, so that they test the first line of defence. The
pool tests use the worker process, the hard limit.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

import pytest
from hagent_app.ha import State
from hagent_app.rules.render_pool import RenderPool
from hagent_app.rules.templates import (
    ScopedStates,
    TemplateClock,
    TemplateEngine,
    TemplateError,
    plain_data,
    render_sync,
)

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
SENSOR_STATES = [State(f"sensor.s{i}", "1", {}, NOW, NOW, {}) for i in range(300)]
SENSORS = ScopedStates.build(SENSOR_STATES, lambda _e: True)
S = "{% set s = 'a' * 250000 %}"
# A regular expression that backtracks: about 2**30 steps in one C call.
STUCK = "{{ ('" + "a" * 30 + "!') is match('(a+)+$') }}"


def sync(source: str, seconds: float = 1.0, view: ScopedStates | None = None) -> str:
    return render_sync(view or ScopedStates(), TemplateClock(), seconds, source, {})


@pytest.fixture
async def pool() -> AsyncIterator[RenderPool]:
    """A pool of its own, so that killed workers do not touch other tests."""
    own = RenderPool(2)
    yield own
    await asyncio.to_thread(own.close)


# ------------------------------------------------------------- in process


def test_h1_a_caught_deadline_never_yields_a_result() -> None:
    """``states.sensor is sequence`` runs inside ``except Exception`` in
    Jinja. The deadline signal must pass it, and a removed trace function
    must fail the render."""
    source = (
        "{% for a in range(400) %}{% for b in range(400) %}"
        "{% if states.sensor is sequence %}{% endif %}{% endfor %}{% endfor %}done"
    )
    for _ in range(3):
        started = time.monotonic()
        with pytest.raises(TemplateError, match="took longer"):
            sync(source, seconds=0.2, view=SENSORS)
        assert time.monotonic() - started < 1.5


@pytest.mark.parametrize(
    "source",
    [
        S + "{% set ns = namespace(l=[s] * 1000) %}{{ ns }}",
        S + "{% set ns = namespace(l=[s] * 50) %}{{ ns|string|length }}",
        S + "{% set ns = namespace(l=[s] * 50) %}{{ (ns ~ '')|length }}",
        S + "{% set ns = namespace(l=[s] * 50) %}{{ ns|to_json|length }}",
        S + "{% set ns = namespace(l=[s] * 50) %}{{ ('%s' % ns)|length }}",
        S + "{{ dict(a=[s] * 50).items()|string|length }}",
        S + "{{ dict(a=[s] * 50).values()|string|length }}",
        S + "{{ dict(a=[s] * 50).keys()|list|length }}{{ dict(a=[s]*50).items() }}",
    ],
)
def test_h2_the_size_walk_sees_namespaces_and_dict_views(source: str) -> None:
    with pytest.raises(TemplateError, match=r"larger than|longer than"):
        sync(source)


@pytest.mark.parametrize(
    "source",
    [
        S + "{% for i in range(1000) %}{{ s }}{% endfor %}",
        S + "{% set b %}{% for i in range(1000) %}{{ s }}{% endfor %}{% endset %}",
    ],
)
def test_h3_the_output_limit_holds_while_jinja_joins(source: str) -> None:
    with pytest.raises(TemplateError, match="output is longer"):
        sync(source)


def test_h3_many_small_parts_stay_within_the_limit() -> None:
    assert len(sync("{% for i in range(100000) %}{{ 'x' }}{% endfor %}")) == 100000
    with pytest.raises(TemplateError, match="output is longer"):
        sync("{% for i in range(100000) %}{{ 'xxx' }}{% endfor %}")


@pytest.mark.parametrize(
    "source",
    [
        "{{ '%*d' % (500000000, 1) }}",
        "{{ '%*d'|format(500000000, 1) }}",
        "{{ '%.*f' % (500000000, 1.0) }}",
        "{{ '%(a)*d' % {'a': 1} }}",
        "{{ 0|timestamp_custom('%*Y') }}",
    ],
)
def test_m2_a_star_width_fails(source: str) -> None:
    with pytest.raises(TemplateError, match=r"of \*"):
        sync(source)


def test_m2_a_normal_format_still_works() -> None:
    assert sync("{{ '%5.1f|%-3d|%%' % (2.25, 7) }}") == "2.2|7  |%"


def test_l1_integer_multiplication_has_a_size_limit() -> None:
    source = (
        "{% set ns = namespace(x=2**1000) %}{% for i in range(40) %}"
        "{% set ns.x = ns.x * ns.x %}{% endfor %}{{ ns.x > 1 }}"
    )
    started = time.monotonic()
    with pytest.raises(TemplateError, match="product is too large"):
        sync(source)
    assert time.monotonic() - started < 0.5
    assert sync("{{ 6 * 7 }}") == "42"


def test_plain_data_holds_no_objects_with_methods() -> None:
    class Kind(StrEnum):
        A = "a"

    value = plain_data({"k": [Kind.A, (1, 2.5), {3}, object(), None, NOW]})
    assert type(value["k"][0]) is str
    assert value["k"][1] == (1, 2.5)
    assert value["k"][2] == [3]
    assert isinstance(value["k"][3], str)
    assert value["k"][4:] == [None, NOW]


def test_the_scope_fingerprint_follows_the_visible_ids() -> None:
    one = ScopedStates.build(SENSOR_STATES, lambda e: e != "sensor.s1")
    two = ScopedStates.build(SENSOR_STATES, lambda e: e != "sensor.s1")
    assert one.fingerprint() == two.fingerprint()
    assert one.fingerprint() != SENSORS.fingerprint()


# ------------------------------------------------------------ worker pool


async def test_the_worker_renders_with_the_scoped_view(pool: RenderPool) -> None:
    view = ScopedStates.build(
        [State("sensor.a", "5", {"unit_of_measurement": "W"}, NOW, NOW, {})],
        lambda _e: True,
    )
    engine = TemplateEngine(view, TemplateClock(), pool=pool)
    text = await engine.render(
        "{{ states('sensor.a', with_unit=True) }} {{ inputs.x }} "
        "{{ states.sensor.a.last_changed.year }} {{ states('sensor.b') }}",
        {"inputs": {"x": 2}},
    )
    assert text == "5 W 2 2026 unknown"
    # The second render reuses the worker and its copy of the view.
    assert await engine.render("{{ states('sensor.a') }}", {}) == "5"
    assert pool.started == 1


async def test_m1_a_stuck_c_call_is_killed_and_the_pool_recovers(
    pool: RenderPool,
) -> None:
    """Review probes p6 and p7: stuck renders must not hold the pool."""
    view, clock = ScopedStates(), TemplateClock()
    started = time.monotonic()
    results = await asyncio.gather(
        *(
            TemplateEngine(view, clock, 0.2, pool=pool, owner=f"a{i}").render(STUCK, {})
            for i in range(5)
        ),
        return_exceptions=True,
    )
    assert all(isinstance(r, TemplateError) for r in results)
    assert all("took longer" in str(r) for r in results)
    assert pool.killed == 5
    engine = TemplateEngine(view, clock, 0.2, pool=pool)
    assert await engine.render("{{ 1 + 1 }}", {}) == "2"
    # Five kills in three rounds of two workers, about 0.7 s each.
    assert time.monotonic() - started < 6


async def test_m1_one_agent_cannot_hold_every_worker(pool: RenderPool) -> None:
    """Review finding M1: an agent with parallel invocations gets at most
    ``per_owner`` workers, so that another agent still renders."""
    assert pool.per_owner == 1
    view, clock = ScopedStates(), TemplateClock()
    greedy = TemplateEngine(view, clock, 0.3, pool=pool, owner="agent-a")
    other = TemplateEngine(view, clock, 0.3, pool=pool, owner="agent-b")
    stuck = [asyncio.create_task(greedy.render(STUCK, {})) for _ in range(2)]
    await asyncio.sleep(0.1)
    started = time.monotonic()
    assert await other.render("{{ 1 + 1 }}", {}) == "2"
    assert time.monotonic() - started < 0.5
    results = await asyncio.gather(*stuck, return_exceptions=True)
    assert all(isinstance(r, TemplateError) for r in results)


async def test_h1_the_worker_ends_an_escaped_render(pool: RenderPool) -> None:
    source = (
        "{% for a in range(4000) %}{% for b in range(400) %}"
        "{% if states.sensor is sequence %}{% endif %}{% endfor %}{% endfor %}done"
    )
    engine = TemplateEngine(SENSORS, TemplateClock(), 0.2, pool=pool)
    started = time.monotonic()
    with pytest.raises(TemplateError, match="took longer"):
        await engine.render(source, {})
    assert time.monotonic() - started < 2


async def test_the_worker_has_a_memory_limit() -> None:
    small = RenderPool(1, memory=32 * 1024 * 1024)
    try:
        engine = TemplateEngine(ScopedStates(), TemplateClock(), 5, pool=small)
        source = (
            "{% set ns = namespace(l=[]) %}{% for i in range(400) %}"
            "{% set ns.l = ns.l + [('a' * 250000) ~ i] %}{% endfor %}"
            "{{ ns.l|length }}"
        )
        with pytest.raises(TemplateError, match=r"memory|stopped"):
            await engine.render(source, {})
        assert small.killed == 1
        assert await engine.render("{{ 'ok' }}", {}) == "ok"
    finally:
        await asyncio.to_thread(small.close)


async def test_the_worker_does_not_get_the_app_environment(
    pool: RenderPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SUPERVISOR_TOKEN", "not-for-the-worker")
    monkeypatch.setenv("TZ", "Europe/London")
    engine = TemplateEngine(ScopedStates(), TemplateClock(), pool=pool)
    assert await engine.render("{{ now().tzname() in ['GMT', 'BST'] }}", {}) == "True"
    (worker,) = pool._idle
    environ, cwd = _process_info(worker.proc.pid)
    assert environ.split(b"\0") == [b"TZ=Europe/London", b""]
    assert cwd == Path("/")


def _process_info(pid: int) -> tuple[bytes, Path]:
    proc = Path(f"/proc/{pid}")
    return (proc / "environ").read_bytes(), (proc / "cwd").readlink()


async def test_a_template_error_keeps_the_worker(pool: RenderPool) -> None:
    engine = TemplateEngine(ScopedStates(), TemplateClock(), pool=pool)
    with pytest.raises(TemplateError, match="unknown names"):
        await engine.render("{{ nope }}", {})
    with pytest.raises(TemplateError, match="output is longer"):
        await engine.render(S + "{% for i in range(1000) %}{{ s }}{% endfor %}", {})
    assert await engine.render("{{ 1 }}", {}) == "1"
    assert (pool.started, pool.killed) == (1, 0)
