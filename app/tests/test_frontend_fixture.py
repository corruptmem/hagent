"""The front-end test fixture holds the current definition defaults.

A front-end test checks that the agent form has a control for each field
of this fixture. This test keeps the fixture equal to
``default_definition_data()`` (the body of ``GET api/agents/defaults``
without the settings budget), so that a new definition field fails the
front-end test until the form has a control for it.
"""

import json
from pathlib import Path

from hagent_app.agents.definition import default_definition_data

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "frontend"
    / "src"
    / "test"
    / "fixtures"
    / "defaults.json"
)


def test_frontend_defaults_fixture_matches_the_api() -> None:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert data == {"definition": default_definition_data()}
