"""Records and errors of the agents area."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from hagent_app.agents.definition import AgentDefinition

type Change = Literal["create", "edit", "copy", "restore"]
type StoreChoice = Literal["keep", "delete"]


@dataclass(frozen=True, slots=True)
class StoreRef:
    """A short reference to a knowledge store."""

    id: str
    name: str


@dataclass(frozen=True, slots=True)
class Agent:
    """An agent and its current definition version.

    ``version`` is the number of the current definition version. A consumer
    that runs an invocation keeps this snapshot for the whole invocation, so
    that a change takes effect only between invocations (design section 9.6).
    """

    id: str
    name: str
    enabled: bool
    version: int
    definition: AgentDefinition
    private_store: StoreRef | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class AgentVersion:
    """One definition version. A version never changes."""

    agent_id: str
    version: int
    definition: AgentDefinition
    change: Change
    source_agent_id: str | None
    source_version: int | None
    created_at: str


class AgentError(Exception):
    """An error of the agents area. ``code`` is a stable identifier."""

    code = "agent_error"

    def __init__(self, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class AgentNotFoundError(AgentError):
    """The agent does not exist, or the user deleted it."""

    code = "agent_not_found"


class VersionNotFoundError(AgentError):
    """The definition version does not exist."""

    code = "version_not_found"


class NameConflictError(AgentError):
    """Another live agent has the same name."""

    code = "name_conflict"


class VersionConflictError(AgentError):
    """The base version of an edit is not the current version."""

    code = "version_conflict"


class DefinitionInvalidError(AgentError):
    """The definition is not valid. ``details`` lists the errors."""

    code = "definition_invalid"


class StoreChoiceRequiredError(AgentError):
    """A delete must say whether to keep or delete the private store."""

    code = "private_store_choice_required"


class StoreInUseError(AgentError):
    """Other agents attach the private store, so it cannot be deleted."""

    code = "private_store_in_use"


@dataclass(frozen=True, slots=True)
class FieldChange:
    """One changed field between two definition versions."""

    path: str
    old: Any = field(default=None)
    new: Any = field(default=None)
