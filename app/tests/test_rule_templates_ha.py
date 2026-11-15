"""The rule template sandbox gives HA's own output (task-13, AC2).

``fixtures/ha_templates/cases.json`` holds the results of HA's
``render_template`` command for synthetic templates. ``record.py`` in the
same folder recorded them from a real HA instance. This test renders each
template in the sandbox and compares. It never needs HA.

The state functions in the fixtures name only entities that do not exist.
The sandbox answers the same for an entity outside the agent's scope, so
these cases also show that an out-of-scope entity looks like a missing one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from hagent_app.rules.templates import (
    ScopedStates,
    TemplateClock,
    TemplateEngine,
    TemplateError,
)

FIXTURES = Path(__file__).parent / "fixtures" / "ha_templates" / "cases.json"
DATA = json.loads(FIXTURES.read_text())


def _normal(value: Any) -> Any:
    """The JSON form, as HA sends it over the WebSocket API."""
    return json.loads(json.dumps(value, default=str))


def test_the_fixtures_come_from_a_real_ha_version() -> None:
    assert DATA["ha_version"]
    assert len(DATA["cases"]) >= 50


@pytest.mark.parametrize("case", DATA["cases"], ids=lambda c: c["name"])
async def test_the_sandbox_matches_ha(case: dict[str, Any]) -> None:
    engine = TemplateEngine(ScopedStates(), TemplateClock())
    if case.get("error"):
        with pytest.raises(TemplateError):
            await engine.render_value(case["template"], case["variables"])
        return
    value = await engine.render_value(case["template"], case["variables"])
    expected = case["result"]
    assert _normal(value) == expected
    # The same type: 3 and 3.0 are equal in Python, but not the same output.
    assert type(_normal(value)) is type(expected)
