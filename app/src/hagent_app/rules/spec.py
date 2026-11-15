"""The rule spec: the strict model of one rule version (design section 10.1).

An agent writes a rule spec with ``self.create`` or ``self.update``. The
model rejects unknown keys. ``rules/checks.py`` checks the spec against the
agent's scope when the agent writes it, and the rule stage checks each part
again when the rule runs.

Spec parts:

- ``match``: a template. The rule applies when it renders true.
- ``context``: data that the runtime collects before the action runs
  (design section 10.5). Each item has a name. Templates read it as
  ``context.<name>``.
- ``action``: a ``template`` action or a ``decision`` action.
- ``result``: a template that builds the final result. It can render
  ``decline``. Without it, the result is the action output.
- ``writes``: service calls on output entities, with data templates.
- ``cache``: the rule result cache.
- ``agreement`` and ``trial``: stored for backtests and trials (task-14).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    model_validator,
)

from hagent_app.rules.templates import MAX_TEMPLATE

Template = Annotated[str, StringConstraints(min_length=1, max_length=MAX_TEMPLATE)]
Name = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
EntityId = Annotated[
    str, StringConstraints(pattern=r"^[a-z0-9_]+\.[a-z0-9_]+$", max_length=255)
]
Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,100}$")]
Service = Annotated[
    str, StringConstraints(pattern=r"^[a-z0-9_]{1,100}\.[a-z0-9_]{1,100}$")
]
StoreId = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{32}$")]
Text = Annotated[str, StringConstraints(min_length=1, max_length=4000)]
Label = Annotated[str, StringConstraints(min_length=1, max_length=200)]
CacheSeconds = Annotated[int, Field(ge=0, le=86_400)]

MAX_CONTEXT = 10
MAX_WRITES = 10
MAX_QUESTIONS = 50


class _Spec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


# ------------------------------------------------------------------ context


class HistoryItem(_Spec):
    """Long-term statistics of entities in the read scope over a window."""

    type: Literal["history"]
    name: Name
    entity_ids: Annotated[list[EntityId], Field(min_length=1, max_length=50)]
    hours: Annotated[int, Field(ge=1, le=720)] = 24
    period: Literal["5minute", "hour", "day"] = "hour"
    cache_seconds: CacheSeconds = 0


class ActionResponseItem(_Spec):
    """The response of an HA action that only returns data."""

    type: Literal["action_response"]
    name: Name
    service: Service
    target: dict[str, JsonValue] | None = None
    data: dict[str, JsonValue] | None = None
    cache_seconds: CacheSeconds = 0


class StoreFileItem(_Spec):
    """The content of a file in an attached knowledge store."""

    type: Literal["store_file"]
    name: Name
    store_id: StoreId
    path: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    section: Annotated[str, StringConstraints(max_length=255)] | None = None
    cache_seconds: CacheSeconds = 0


class InvocationHistoryItem(_Spec):
    """A summary of the agent's recent invocations from the same source."""

    type: Literal["invocation_history"]
    name: Name
    limit: Annotated[int, Field(ge=1, le=50)] = 10
    cache_seconds: CacheSeconds = 0


ContextItem = Annotated[
    HistoryItem | ActionResponseItem | StoreFileItem | InvocationHistoryItem,
    Field(discriminator="type"),
]


# ------------------------------------------------------------------ actions


class TemplateAction(_Spec):
    """The action output is the rendered template (design section 10.2)."""

    type: Literal["template"]
    template: Template


class YesNoSpec(_Spec):
    """A yes/no question. The answer is the probability of yes."""

    type: Literal["yes_no"]
    name: Name
    text: Text
    yes: Label | None = None
    no: Label | None = None


class ChoiceSpec(_Spec):
    """A choice question with 2 to 255 named options."""

    type: Literal["choice"]
    name: Name
    text: Text
    options: Annotated[dict[Label, Label], Field(min_length=2, max_length=255)]


class LevelSpec(_Spec):
    """One level of a score question."""

    label: Label
    description: Annotated[str, StringConstraints(max_length=200)] = ""


class ScoreSpec(_Spec):
    """A score question with 2 to 10 ordered levels (low first)."""

    type: Literal["score"]
    name: Name
    text: Text
    levels: Annotated[list[LevelSpec], Field(min_length=2, max_length=10)]


QuestionSpec = Annotated[
    YesNoSpec | ChoiceSpec | ScoreSpec, Field(discriminator="type")
]
Selection = bool | Annotated[list[Label], Field(max_length=100)]


class Include(_Spec):
    """The values that the decision request includes.

    ``true`` includes all, ``false`` none, and a list names the input
    entity IDs, the caller data keys or the context item names.
    """

    inputs: Selection = True
    data: Selection = True
    context: Selection = True


class DecisionAction(_Spec):
    """One decision request with one or more questions (design 10.3)."""

    type: Literal["decision"]
    questions: Annotated[
        list[QuestionSpec], Field(min_length=1, max_length=MAX_QUESTIONS)
    ]
    include: Include = Include()

    @model_validator(mode="after")
    def _unique(self) -> Self:
        names = [q.name for q in self.questions]
        if len(set(names)) != len(names):
            msg = "question names must be unique"
            raise ValueError(msg)
        return self


Action = Annotated[TemplateAction | DecisionAction, Field(discriminator="type")]


# --------------------------------------------------------- writes and more


class WriteSpec(_Spec):
    """One service call on an output entity, built from the result.

    ``service`` and ``entity_id`` are fixed. Each value in ``data`` is a
    template. It receives ``result`` and the other variables.
    """

    service: Service
    entity_id: EntityId
    data: Annotated[dict[Slug, Template], Field(max_length=20)] = Field(
        default_factory=dict
    )


class CacheSpec(_Spec):
    """The rule result cache (design section 10.1).

    ``keys`` name input entity IDs, or caller data keys as ``data.<key>``.
    The runtime returns the previous result while the values of these keys
    stay the same and the window has not passed.
    """

    seconds: Annotated[int, Field(ge=1, le=86_400)]
    keys: Annotated[list[Label], Field(max_length=20)] = Field(default_factory=list)


class TrialSpec(_Spec):
    """Trials (task-14): an agent turn on a sample of matches."""

    enabled: bool = False
    rate: Annotated[float, Field(gt=0, le=1)] = 0.2


class RuleSpec(_Spec):
    """One rule version. Unknown keys fail."""

    name: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    description: Annotated[str, StringConstraints(max_length=2000)] | None = None
    match: Template
    context: Annotated[list[ContextItem], Field(max_length=MAX_CONTEXT)] = Field(
        default_factory=list
    )
    action: Action
    result: Template | None = None
    writes: Annotated[list[WriteSpec], Field(max_length=MAX_WRITES)] = Field(
        default_factory=list
    )
    cache: CacheSpec | None = None
    agreement: Template | None = None
    trial: TrialSpec | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        names = [item.name for item in self.context]
        if len(set(names)) != len(names):
            msg = "context item names must be unique"
            raise ValueError(msg)
        return self

    def templates(self) -> list[tuple[str, str]]:
        """Every template of the spec, with its place, for the static check."""
        found = [("match", self.match)]
        if isinstance(self.action, TemplateAction):
            found.append(("action.template", self.action.template))
        if self.result is not None:
            found.append(("result", self.result))
        for i, write in enumerate(self.writes):
            found += [(f"writes.{i}.data.{k}", v) for k, v in write.data.items()]
        if self.agreement is not None:
            found.append(("agreement", self.agreement))
        return found

    def to_data(self) -> dict[str, Any]:
        """The JSON form that the store keeps."""
        return self.model_dump(mode="json", exclude_none=True)
