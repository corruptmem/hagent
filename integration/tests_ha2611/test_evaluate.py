"""Tests of the AI Task EVALUATE feature against HA Core 2026.11.1.

No released pytest-homeassistant-custom-component supports HA Core 2026.11
yet. These tests therefore call the entity directly, with a fake runtime in
place of the link. The tests in tests/ cover the link and the HA set-up.
"""

from __future__ import annotations

from typing import Any

import pytest
from custom_components.hagent.ai_task import (
    EVALUATE,
    HagentAITaskEntity,
    evaluation_answers,
    evaluation_schema,
)
from custom_components.hagent.invoke import HagentInvocationError
from homeassistant.components import ai_task

QUESTIONS: dict[str, Any] = {
    "open": {"type": "noul", "instructions": "Is a window open?"},
    "mode": {
        "type": "choice",
        "instructions": "Which heating mode fits?",
        "criteria": {"eco": "Nobody home", "comfort": "Someone home"},
    },
    "risk": {
        "type": "score",
        "instructions": "How high is the frost risk?",
        "criteria": ["low", "medium", "high"],
    },
}

ANSWERS: dict[str, Any] = {
    "open": {"noul": 0.2},
    "mode": {"choice": "eco", "probabilities": {"eco": 0.7, "comfort": 0.3}},
    "risk": {"score": 0.4, "probabilities": [0.6, 0.4, 0.0]},
}


class FakeRuntime:
    """The parts of ``HagentRuntime`` that the entity uses."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.agents = {"agent-1": {"id": "agent-1", "name": "Heating", "version": 1}}
        self.hub_identifier = ("hagent", "app_entry")
        self.connected = True
        self.data = data
        self.calls: list[dict[str, Any]] = []

    async def invoke(self, fields: dict[str, Any]) -> Any:
        self.calls.append(fields)
        return self.data


def ok(result: Any) -> dict[str, Any]:
    return {"invocation_id": "i", "status": "ok", "result": result, "path": "rule"}


def test_the_entity_offers_evaluate() -> None:
    assert EVALUATE is ai_task.AITaskEntityFeature.EVALUATE
    entity = HagentAITaskEntity(FakeRuntime(ok(None)), "agent-1")  # type: ignore[arg-type]
    assert ai_task.AITaskEntityFeature.EVALUATE in entity.supported_features
    assert ai_task.AITaskEntityFeature.GENERATE_DATA in entity.supported_features


async def test_evaluate_sends_the_questions_and_returns_answers() -> None:
    runtime = FakeRuntime(ok(ANSWERS))
    entity = HagentAITaskEntity(runtime, "agent-1")  # type: ignore[arg-type]
    task = ai_task.EvaluationTask(
        name="heating check", questions=QUESTIONS, state={"temp": 4}
    )
    result = await entity._async_evaluate(task)
    assert result.answers == {
        "open": ai_task.NoulAnswer(noul=0.2),
        "mode": ai_task.ChoiceAnswer(
            choice="eco", probabilities={"eco": 0.7, "comfort": 0.3}
        ),
        "risk": ai_task.ScoreAnswer(score=0.4, probabilities=[0.6, 0.4, 0.0]),
    }
    [call] = runtime.calls
    assert call["source"] == "ai_task_evaluate"
    assert call["agent_id"] == "agent-1"
    assert call["data"] == {
        "task_name": "heating check",
        "questions": QUESTIONS,
        "state": {"temp": 4},
    }
    assert call["result_schema"] == evaluation_schema(QUESTIONS)
    assert call["on_timeout"] == "cancel"


def test_evaluation_schema_asks_for_one_answer_per_question() -> None:
    schema = evaluation_schema(QUESTIONS)
    assert schema["type"] == "object"
    assert schema["required"] == ["open", "mode", "risk"]
    assert schema["additionalProperties"] is False
    mode = schema["properties"]["mode"]["properties"]
    assert mode["choice"]["enum"] == ["eco", "comfort"]
    risk = schema["properties"]["risk"]["properties"]
    assert risk["score"]["maximum"] == 2
    assert risk["probabilities"]["minItems"] == 3


@pytest.mark.parametrize(
    "result",
    [None, "text", {"open": {"noul": 0.2}}, {**ANSWERS, "open": {"noul": "x"}}],
)
def test_a_result_that_does_not_fit_raises(result: Any) -> None:
    with pytest.raises(HagentInvocationError):
        evaluation_answers(QUESTIONS, result)


async def test_an_error_result_raises() -> None:
    runtime = FakeRuntime(
        {"status": "error", "error": {"code": "agent_off", "message": "off"}}
    )
    entity = HagentAITaskEntity(runtime, "agent-1")  # type: ignore[arg-type]
    task = ai_task.EvaluationTask(name="t", questions=QUESTIONS, state=1)
    with pytest.raises(HagentInvocationError, match="agent_off"):
        await entity._async_evaluate(task)
