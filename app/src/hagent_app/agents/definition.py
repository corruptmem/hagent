"""The agent definition: the fields of design section 7.1, with validation.

An agent definition is the configuration that the user owns. Each change
creates a new version (``agents/store.py``). The models are frozen, and every
collection is a tuple, so that a reader cannot change a definition in place.

Validation checks formats and the rules between fields. It does not check
that HA entities exist, because HA can be down and entities come and go.
The service layer checks the knowledge-store IDs against the database.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    model_validator,
)

# Patterns. HA object IDs use lower-case letters, digits and underscores.
_ENTITY_ID = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
_SLUG = re.compile(r"^[a-z0-9_]+$")
_SERVICE = re.compile(r"^(?P<domain>[a-z0-9_]+)\.(?P<service>[a-z0-9_]+|\*)$")
_REGISTRY_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_STORE_ID = re.compile(r"^[a-f0-9]{32}$")
_TOOL_REF = re.compile(r"^(?P<group>[a-z_]+)(\.(?P<tool>[a-z_]+|\*))?$")
_CRON_FIELD = re.compile(r"^[0-9*/,-]+$")

TOOL_GROUPS = ("self", "knowledge", "memory", "ha")

# Services that run other actions. A ``read`` or ``control`` scope permits
# one only when it names the service (design section 8). A wildcard such as
# ``script.*`` does not permit them.
INDIRECT_DOMAINS = frozenset(
    {
        "script",
        "automation",
        "scene",
        "homeassistant",
        "shell_command",
        "rest_command",
        "python_script",
        "pyscript",
        "hassio",
        "notify",
    }
)

MAX_LIST = 500


def _match(pattern: re.Pattern[str], what: str) -> Any:
    def check(value: str) -> str:
        if not pattern.match(value):
            msg = f"not a valid {what}: {value!r}"
            raise ValueError(msg)
        return value

    return AfterValidator(check)


def _no_duplicates[T](values: tuple[T, ...]) -> tuple[T, ...]:
    if len(set(values)) != len(values):
        msg = "the list contains duplicates"
        raise ValueError(msg)
    return values


def _cron(value: str) -> str:
    parts = value.split()
    if len(parts) != 5 or not all(_CRON_FIELD.match(p) for p in parts):  # noqa: PLR2004
        msg = f"not a 5-field cron expression: {value!r}"
        raise ValueError(msg)
    return value


EntityId = Annotated[str, _match(_ENTITY_ID, "entity ID")]
Slug = Annotated[str, _match(_SLUG, "identifier")]
RegistryId = Annotated[str, _match(_REGISTRY_ID, "registry ID")]
ServiceName = Annotated[str, _match(_SERVICE, "service name (domain.service)")]
StoreId = Annotated[str, _match(_STORE_ID, "knowledge store ID")]
ToolRef = Annotated[str, _match(_TOOL_REF, "tool reference")]
Cron = Annotated[str, AfterValidator(_cron)]
ModelName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]


EntityList = Annotated[
    tuple[EntityId, ...], Field(max_length=MAX_LIST), AfterValidator(_no_duplicates)
]
RegistryList = Annotated[
    tuple[RegistryId, ...], Field(max_length=MAX_LIST), AfterValidator(_no_duplicates)
]
SlugList = Annotated[
    tuple[Slug, ...], Field(max_length=MAX_LIST), AfterValidator(_no_duplicates)
]
ServiceList = Annotated[
    tuple[ServiceName, ...], Field(max_length=MAX_LIST), AfterValidator(_no_duplicates)
]
ToolRefList = Annotated[
    tuple[ToolRef, ...], Field(max_length=MAX_LIST), AfterValidator(_no_duplicates)
]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


# ---------------------------------------------------------------- models


class ModelRef(_Model):
    """A model and its provider (design section 16)."""

    provider: Literal["openai", "openrouter"]
    model: ModelName


class Models(_Model):
    """The chat model, the decision model and the summary model.

    ``summary`` is the low-cost model for compaction summaries. If it is not
    set, the runtime uses ``chat``. Read it with ``summary_model``.
    """

    chat: ModelRef
    decision: ModelRef | None = None
    summary: ModelRef | None = None

    @property
    def summary_model(self) -> ModelRef:
        """The model for compaction summaries."""
        return self.summary or self.chat


class Inputs(_Model):
    """Entities and devices whose current values go into every invocation."""

    entities: EntityList = ()
    devices: RegistryList = ()


class ResultType(_Model):
    """The result type: ``none``, ``text``, or ``structured`` with a schema."""

    type: Literal["none", "text", "structured"] = "text"
    schema_: dict[str, JsonValue] | None = Field(default=None, alias="schema")

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, populate_by_name=True
    )

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.type == "structured":
            if self.schema_ is None:
                msg = "a structured result needs a JSON schema"
                raise ValueError(msg)
            if self.schema_.get("type") != "object":
                msg = 'the result schema must have "type": "object"'
                raise ValueError(msg)
        elif self.schema_ is not None:
            msg = f"a {self.type!r} result takes no schema"
            raise ValueError(msg)
        return self


class HAScope(_Model):
    """The targets that a ``read`` or ``control`` level permits."""

    all: bool = False
    entities: EntityList = ()
    devices: RegistryList = ()
    areas: RegistryList = ()
    floors: RegistryList = ()
    labels: RegistryList = ()
    domains: SlugList = ()

    @property
    def empty(self) -> bool:
        """True if the scope permits nothing."""
        return not (
            self.all
            or self.entities
            or self.devices
            or self.areas
            or self.floors
            or self.labels
            or self.domains
        )

    @model_validator(mode="after")
    def _check(self) -> Self:
        lists = (
            self.entities,
            self.devices,
            self.areas,
            self.floors,
            self.labels,
            self.domains,
        )
        if self.all and any(lists):
            msg = "a scope with all=true takes no lists"
            raise ValueError(msg)
        return self


class HATools(_Model):
    """The ``ha`` tool group: a level and its scope (design section 8)."""

    level: Literal["none", "read", "control", "admin"] = "none"
    scope: HAScope = HAScope()
    services: ServiceList = ()

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.level in {"none", "admin"}:
            if not self.scope.empty or self.services:
                msg = f"level {self.level!r} takes no scope and no services"
                raise ValueError(msg)
            return self
        if self.scope.empty:
            msg = "the scope must set all=true or list at least one target"
            raise ValueError(msg)
        if self.services and self.level != "control":
            msg = "only level 'control' takes services"
            raise ValueError(msg)
        for name in self.services:
            match = _SERVICE.match(name)
            assert match is not None  # noqa: S101 - checked by ServiceName
            if match["domain"] in INDIRECT_DOMAINS and match["service"] == "*":
                msg = (
                    f"{name!r}: name each {match['domain']} service; "
                    "a wildcard does not permit indirect services"
                )
                raise ValueError(msg)
        return self


class Tools(_Model):
    """The tool groups that the agent can use (design section 8)."""

    self_: bool = Field(default=False, alias="self")
    knowledge: bool = True
    memory: bool = True
    ha: HATools = HATools()

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, populate_by_name=True
    )

    def enabled(self, group: str) -> bool:
        """True if the tool group is on."""
        if group == "self":
            return self.self_
        if group == "ha":
            return self.ha.level != "none"
        return bool(getattr(self, group))


class KnowledgeAttachment(_Model):
    """One attached knowledge store (design section 13.4)."""

    store_id: StoreId
    access: Literal["read", "read_write"] = "read"
    inject_main: bool = False


class Retrieval(_Model):
    """Automatic recall (design section 13.3)."""

    mode: Literal["off", "assist", "all"] = "off"
    result_count: int = Field(default=5, ge=1, le=50)
    token_limit: int = Field(default=1000, ge=100, le=20000)


class Approval(_Model):
    """Tools, services and entities that need approval for each call."""

    tools: ToolRefList = ()
    services: ServiceList = ()
    entities: EntityList = ()


class Assist(_Model):
    """Assist exposure (design section 15). Off by default."""

    enabled: bool = False


class Budget(_Model):
    """The daily cost limit and the limits for one agent turn."""

    daily_usd: float = Field(default=0.50, ge=0, le=1000)
    max_tool_calls: int = Field(default=20, ge=1, le=500)
    max_tokens: int = Field(default=200_000, ge=1000, le=10_000_000)


class Concurrency(_Model):
    """What happens when an invocation arrives while another runs."""

    mode: Literal["queued", "single", "restart", "parallel"] = "queued"
    queue_size: int = Field(default=10, ge=1, le=100)


class Fallback(_Model):
    """What the agent returns when it cannot run an agent turn."""

    mode: Literal["last_result", "fixed", "no_change", "error"] = "last_result"
    value: JsonValue = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.mode == "fixed" and self.value is None:
            msg = "a fixed fallback needs a value"
            raise ValueError(msg)
        if self.mode != "fixed" and self.value is not None:
            msg = f"fallback {self.mode!r} takes no value"
            raise ValueError(msg)
        return self


class Scheduling(_Model):
    """Limits for self-scheduled invocations (design section 12)."""

    min_interval_seconds: int = Field(default=60, ge=1, le=86_400)
    max_schedules: int = Field(default=20, ge=0, le=1000)
    debounce_seconds: float = Field(default=5.0, ge=0, le=3600)


class Review(_Model):
    """The review schedule (design section 14). Daily by default."""

    enabled: bool = True
    cron: Cron = "0 4 * * *"


Name = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)
]
Instructions = Annotated[str, StringConstraints(min_length=1, max_length=100_000)]


class AgentDefinition(_Model):
    """One version of an agent definition (design section 7.1)."""

    name: Name
    instructions: Instructions
    models: Models
    inputs: Inputs = Inputs()
    outputs: EntityList = ()
    result: ResultType = ResultType()
    tools: Tools = Tools()
    knowledge: Annotated[tuple[KnowledgeAttachment, ...], Field(max_length=100)] = ()
    retrieval: Retrieval = Retrieval()
    approval: Approval = Approval()
    assist: Assist = Assist()
    budget: Budget = Budget()
    concurrency: Concurrency = Concurrency()
    fallback: Fallback = Fallback()
    scheduling: Scheduling = Scheduling()
    review: Review = Review()

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.concurrency.mode == "parallel" and self.outputs:
            msg = "concurrency 'parallel' is only for agents with no outputs"
            raise ValueError(msg)
        store_ids = [a.store_id for a in self.knowledge]
        if len(set(store_ids)) != len(store_ids):
            msg = "a knowledge store is attached twice"
            raise ValueError(msg)
        for ref in self.approval.tools:
            match = _TOOL_REF.match(ref)
            assert match is not None  # noqa: S101 - checked by ToolRef
            group = match["group"]
            if group not in TOOL_GROUPS:
                msg = f"approval: unknown tool group {group!r}"
                raise ValueError(msg)
            if not self.tools.enabled(group):
                msg = f"approval: tool group {group!r} is off"
                raise ValueError(msg)
        if (self.approval.services or self.approval.entities) and not (
            self.tools.ha.level in {"control", "admin"} or self.outputs
        ):
            msg = "approval of services or entities needs HA control or outputs"
            raise ValueError(msg)
        return self

    def to_json(self) -> str:
        """The canonical JSON text, with field aliases."""
        return self.model_dump_json(by_alias=True)

    def to_data(self) -> dict[str, Any]:
        """The JSON-compatible data, with field aliases."""
        return self.model_dump(mode="json", by_alias=True)

    @classmethod
    def from_json(cls, text: str | bytes) -> AgentDefinition:
        """Parse and validate a definition from JSON text."""
        return cls.model_validate_json(text, by_alias=True)


def default_definition_data() -> dict[str, Any]:
    """The defaults of every optional field, for a new-agent form."""
    sample = AgentDefinition(
        name="New agent",
        instructions="Describe what the agent is for.",
        models=Models(chat=ModelRef(provider="openai", model="gpt-5-mini")),
    )
    return sample.to_data()
