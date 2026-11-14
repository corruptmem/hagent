"""No agent tool can write a definition (design section 7, task-7 AC4).

The runtime gives ``AgentReader`` to every consumer that is not the
management API, for example the tool executor (task-8). These tests prove
that the reader has no write path and that its results are immutable.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from hagent_app.agents import AgentReader, AgentService
from hagent_app.agents.definition import AgentDefinition
from hagent_app.http import ChangeEvents
from hagent_app.storage import Database
from helpers.agents import definition
from pydantic import ValidationError


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = await Database.open(tmp_path / "hagent.db")
    try:
        yield database
    finally:
        await database.close()


def _defn(**changes: object) -> AgentDefinition:
    return AgentDefinition.from_json(json.dumps(definition(**changes)))


def test_reader_exposes_only_read_methods() -> None:
    public = {
        name for name, _ in inspect.getmembers(AgentReader) if not name.startswith("_")
    }
    assert public == {"get", "get_version", "list_agents"}
    # Slots stop a caller from adding a write method to an instance.
    assert AgentReader.__slots__ == ("_db",)


def test_reader_is_not_a_service() -> None:
    assert not issubclass(AgentReader, AgentService)
    assert not hasattr(AgentReader, "create")
    assert not hasattr(AgentReader, "update")


async def test_reader_results_are_immutable(db: Database) -> None:
    service = AgentService(db, ChangeEvents())
    created = await service.create(
        _defn(
            outputs=["climate.x"],
            result={"type": "structured", "schema": {"type": "object"}},
        )
    )
    reader = AgentReader(db)
    agent = await reader.get(created.id)
    assert agent is not None
    original = agent.definition.to_json()

    with pytest.raises(AttributeError):
        agent.version = 99  # type: ignore[misc]
    with pytest.raises(ValidationError):
        agent.definition.budget.daily_usd = 99  # type: ignore[misc]
    with pytest.raises(ValidationError):
        agent.definition.tools.ha.level = "admin"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        agent.definition.outputs.append("light.y")  # type: ignore[attr-defined]
    # The schema is a plain dict. A change to it stays in the caller's copy.
    assert agent.definition.result.schema_ is not None
    agent.definition.result.schema_["type"] = "array"

    again = await reader.get(created.id)
    assert again is not None
    assert again.definition.to_json() == original
    assert again.version == 1
    version = await reader.get_version(created.id, 1)
    assert version is not None
    assert version.definition.to_json() == original


async def test_reader_reads_deleted_versions(db: Database) -> None:
    service = AgentService(db, ChangeEvents())
    created = await service.create(_defn())
    await service.delete(created.id, "keep")
    reader = AgentReader(db)
    assert await reader.get(created.id) is None
    assert await reader.list_agents() == []
    version = await reader.get_version(created.id, 1)
    assert version is not None
    assert version.definition.name == "Heating"
