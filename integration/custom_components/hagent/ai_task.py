"""The AI Task entity of each agent (design section 9.3).

- ``GENERATE_DATA``: the task becomes an invocation with the source
  ``ai_task``. A task structure becomes the result schema.
- ``SUPPORT_ATTACHMENTS``: the integration reads the attachment files and
  sends them through the link. The app gives them to the model.
- ``EVALUATE``: HA Core 2026.11 adds this feature. The task becomes an
  invocation with the source ``ai_task_evaluate``. The result schema asks
  for one answer per question. On an older HA Core the feature does not
  exist, and the entity does not offer it.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

import probatio
from homeassistant.components import ai_task, conversation
from homeassistant.core import Context, HomeAssistant, callback
from homeassistant.helpers import llm
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import HagentAgentEntity, HagentConfigEntry, setup_agent_platform
from .invoke import (
    HagentInvocationError,
    async_check_admin,
    async_read_attachments,
    link_error,
    result_data,
)
from .link import LinkError
from .runtime import HagentRuntime

if TYPE_CHECKING:
    from homeassistant.components.ai_task import (
        EvaluationAnswer,
        EvaluationTask,
        EvaluationTaskResult,
    )

# An AI Task has no timeout of its own. The app cancels a task that runs
# longer than this.
TASK_TIMEOUT = 300.0
# None on HA Core releases before 2026.11.
EVALUATE = getattr(ai_task.AITaskEntityFeature, "EVALUATE", None)

# The context of the AI Task call that runs in the current asyncio task. HA
# passes no context to ``_async_generate_data`` and ``_async_evaluate``.
_CALLER: ContextVar[Context | None] = ContextVar("hagent_ai_task_caller", default=None)

EVALUATE_TEXT = (
    "Answer each question in data.questions about data.state and the "
    "attachments. Give probabilities from 0 to 1."
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HagentConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the AI Task entity of each agent."""

    def factory(runtime: HagentRuntime, agent_id: str) -> list[Entity]:
        return [HagentAITaskEntity(runtime, agent_id)]

    setup_agent_platform(hass, entry, async_add_entities, "ai_task", factory)


def structure_schema(structure: probatio.Schema) -> dict[str, Any]:
    """The JSON schema of an AI Task structure."""
    schema: dict[str, Any] = probatio.to_openapi(
        structure,
        custom_serializer=llm.selector_serializer,
        openapi_version="3.1.0",
    )
    return schema


def evaluation_schema(questions: dict[str, Any]) -> dict[str, Any]:
    """The JSON schema of the answers to evaluation questions."""
    properties: dict[str, Any] = {}
    for qid, question in questions.items():
        kind = question["type"]
        if kind == "noul":
            answer: dict[str, Any] = {
                "type": "object",
                "properties": {"noul": {"type": "number", "minimum": 0, "maximum": 1}},
                "required": ["noul"],
            }
        elif kind == "choice":
            keys = list(question["criteria"])
            answer = {
                "type": "object",
                "properties": {
                    "choice": {"type": "string", "enum": keys},
                    "probabilities": {
                        "type": "object",
                        "properties": {
                            k: {"type": "number", "minimum": 0, "maximum": 1}
                            for k in keys
                        },
                        "required": keys,
                        "additionalProperties": False,
                    },
                },
                "required": ["choice", "probabilities"],
            }
        else:
            count = len(question["criteria"])
            answer = {
                "type": "object",
                "properties": {
                    "score": {"type": "number", "minimum": 0, "maximum": count - 1},
                    "probabilities": {
                        "type": "array",
                        "items": {"type": "number", "minimum": 0, "maximum": 1},
                        "minItems": count,
                        "maxItems": count,
                    },
                },
                "required": ["score", "probabilities"],
            }
        properties[qid] = answer
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def evaluation_answers(
    questions: dict[str, Any], result: Any
) -> dict[str, EvaluationAnswer]:
    """Convert the structured result into HA evaluation answers."""
    if not isinstance(result, dict):
        msg = "hagent: the evaluation result is not an object"
        raise HagentInvocationError(msg)
    answers: dict[str, EvaluationAnswer] = {}
    try:
        for qid, question in questions.items():
            raw = result[qid]
            kind = question["type"]
            if kind == "noul":
                answers[qid] = ai_task.NoulAnswer(noul=float(raw["noul"]))
            elif kind == "choice":
                answers[qid] = ai_task.ChoiceAnswer(
                    choice=str(raw["choice"]),
                    probabilities={
                        str(k): float(v) for k, v in raw["probabilities"].items()
                    },
                )
            else:
                answers[qid] = ai_task.ScoreAnswer(
                    score=float(raw["score"]),
                    probabilities=[float(v) for v in raw["probabilities"]],
                )
    except (KeyError, TypeError, ValueError, AttributeError) as err:
        msg = f"hagent: the evaluation result does not fit the questions: {err!r}"
        raise HagentInvocationError(msg) from err
    return answers


class HagentAITaskEntity(HagentAgentEntity, ai_task.AITaskEntity):
    """The AI Task entity of one agent."""

    _attr_translation_key = "ai_task"

    def __init__(self, runtime: HagentRuntime, agent_id: str) -> None:
        super().__init__(runtime, agent_id, "ai_task")
        features = (
            ai_task.AITaskEntityFeature.GENERATE_DATA
            | ai_task.AITaskEntityFeature.SUPPORT_ATTACHMENTS
        )
        if EVALUATE is not None:
            features |= EVALUATE
        self._attr_supported_features = features

    @callback
    def async_set_context(self, context: Context) -> None:
        """Keep the caller context for the admin check of this call.

        HA calls this with the call context at the start of each task, in
        the task that then runs ``_async_generate_data`` or
        ``_async_evaluate``. A context variable keeps the value apart from
        concurrent calls, which run in other asyncio tasks.
        """
        super().async_set_context(context)
        _CALLER.set(context)

    async def _check_caller(self) -> None:
        """Apply the admin rule to the caller of this task (docs/link.md)."""
        context = _CALLER.get()
        _CALLER.set(None)
        await async_check_admin(self.hass, context)

    async def _invoke(self, fields: dict[str, Any]) -> Any:
        try:
            data = await self.runtime.invoke(
                {
                    "agent_id": self.agent_id,
                    "wait": True,
                    "timeout": TASK_TIMEOUT,
                    "on_timeout": "cancel",
                    **fields,
                }
            )
        except LinkError as err:
            raise link_error(err) from err
        return result_data(data)

    async def _async_generate_data(
        self,
        task: ai_task.GenDataTask,
        chat_log: conversation.ChatLog,
    ) -> ai_task.GenDataTaskResult:
        """Run the task as an ``ai_task`` invocation."""
        await self._check_caller()
        attachments = await async_read_attachments(self.hass, task.attachments)
        fields: dict[str, Any] = {
            "source": "ai_task",
            "text": task.instructions,
            "data": {"task_name": task.name},
            "attachments": attachments,
            "caller": {"conversation_id": chat_log.conversation_id},
        }
        if task.structure is not None:
            fields["result_schema"] = structure_schema(task.structure)
        result = await self._invoke(fields)
        if task.structure is None and not isinstance(result, str):
            result = "" if result is None else str(result)
        return ai_task.GenDataTaskResult(
            conversation_id=chat_log.conversation_id, data=result
        )

    async def _async_evaluate(self, task: EvaluationTask) -> EvaluationTaskResult:
        """Run the questions as an ``ai_task_evaluate`` invocation."""
        await self._check_caller()
        questions = {k: dict(v) for k, v in task.questions.items()}
        attachments = await async_read_attachments(self.hass, task.attachments)
        result = await self._invoke(
            {
                "source": "ai_task_evaluate",
                "text": EVALUATE_TEXT,
                "data": {
                    "task_name": task.name,
                    "questions": questions,
                    "state": task.state,
                },
                "attachments": attachments,
                "result_schema": evaluation_schema(questions),
            }
        )
        return ai_task.EvaluationTaskResult(
            answers=evaluation_answers(questions, result)
        )
