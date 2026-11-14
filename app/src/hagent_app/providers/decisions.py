"""The decision adapter (design section 10.3).

A **decision model** answers typed questions about some values with
probabilities. hagent calls two decision endpoints directly over HTTP:

- OpenAI: ``POST /v1/decisions`` (model ``gpt-6-luna``).
- OpenRouter: ``POST /api/alpha/decisions`` (Jev models). This endpoint is
  in alpha, so the parser accepts missing optional fields.

The two APIs use different shapes. This module turns one
``DecisionRequest`` into either shape, and turns either response into one
``DecisionResult``. There are three question types: yes/no, choice and
score. A refusal applies to one question only.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from hagent_app.providers.config import Provider, ProvidersConfig
from hagent_app.providers.errors import ProviderResponseError
from hagent_app.providers.http import ProviderHTTP
from hagent_app.providers.meter import CallSpec, Meter
from hagent_app.providers.prices import TokenUsage

MAX_QUESTIONS = 400
CHOICE_LIMITS = (2, 255)
LEVEL_LIMITS = (2, 10)
# A conservative estimate of characters per token, for the reservation.
CHARS_PER_TOKEN = 3


@dataclass(frozen=True, slots=True)
class DecisionModel:
    """A decision model and the provider that serves it."""

    provider: Provider
    model: str


# The OpenRouter model list does not include the Jev decision models, so
# hagent keeps its own list. The ``decision_models`` option extends it.
DEFAULT_DECISION_MODELS: tuple[DecisionModel, ...] = (
    DecisionModel("openai", "gpt-6-luna"),
    DecisionModel("openrouter", "typesafe/jev-1.14"),
    DecisionModel("openrouter", "typesafe/jev-1.13"),
)


def decision_models(config: ProvidersConfig) -> list[DecisionModel]:
    """The default decision models plus the models from the options."""
    models = list(DEFAULT_DECISION_MODELS)
    for raw in config.decision_models:
        provider = raw.get("provider")
        model = raw.get("model")
        if provider not in ("openai", "openrouter") or not isinstance(model, str):
            msg = (
                f"decision model needs provider (openai or openrouter) and model: {raw}"
            )
            raise ValueError(msg)
        entry = DecisionModel(provider, model)
        if entry not in models:
            models.append(entry)
    return models


# Questions


@dataclass(frozen=True, slots=True)
class YesNoQuestion:
    """A yes/no question. ``yes`` and ``no`` describe each answer."""

    name: str
    instructions: str
    yes: str | None = None
    no: str | None = None


@dataclass(frozen=True, slots=True)
class ChoiceQuestion:
    """A choice question. ``options`` maps each option to its description."""

    name: str
    instructions: str
    options: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class Level:
    """One level of a score question."""

    label: str
    description: str = ""


@dataclass(frozen=True, slots=True)
class ScoreQuestion:
    """A score question. ``levels`` go from low (0) to high."""

    name: str
    instructions: str
    levels: Sequence[Level]


type Question = YesNoQuestion | ChoiceQuestion | ScoreQuestion


@dataclass(frozen=True, slots=True)
class DecisionRequest:
    """One decision request. All questions see the same ``values``."""

    model: DecisionModel
    values: str | Mapping[str, Any] | Sequence[Any]
    questions: Sequence[Question]

    def validate(self) -> None:
        """Raise ``ValueError`` when the request breaks a provider limit."""
        if not 1 <= len(self.questions) <= MAX_QUESTIONS:
            msg = f"a request needs 1 to {MAX_QUESTIONS} questions"
            raise ValueError(msg)
        names = [q.name for q in self.questions]
        if len(set(names)) != len(names) or not all(names):
            msg = "question names must be unique and not empty"
            raise ValueError(msg)
        for q in self.questions:
            if isinstance(q, ChoiceQuestion):
                low, high = CHOICE_LIMITS
                if not low <= len(q.options) <= high:
                    msg = f"choice question {q.name!r} needs {low} to {high} options"
                    raise ValueError(msg)
            elif isinstance(q, ScoreQuestion):
                low, high = LEVEL_LIMITS
                if not low <= len(q.levels) <= high:
                    msg = f"score question {q.name!r} needs {low} to {high} levels"
                    raise ValueError(msg)


# Answers


@dataclass(frozen=True, slots=True)
class YesNoAnswer:
    """The probability of yes, from 0 to 1."""

    name: str
    probability: float


@dataclass(frozen=True, slots=True)
class ChoiceAnswer:
    """The most likely option, a probability for each option, a confidence."""

    name: str
    choice: str
    probabilities: Mapping[str, float]
    confidence: float | None


@dataclass(frozen=True, slots=True)
class ScoreAnswer:
    """The probability-weighted mean level, level probabilities, a confidence.

    ``probabilities`` maps each level label to its probability.
    """

    name: str
    score: float
    probabilities: Mapping[str, float]
    confidence: float | None


@dataclass(frozen=True, slots=True)
class Refusal:
    """The model refused this question. Other questions still have answers."""

    name: str


type Answer = YesNoAnswer | ChoiceAnswer | ScoreAnswer | Refusal


@dataclass(frozen=True, slots=True)
class DecisionResult:
    """The answers, keyed by question name, and the usage of the call."""

    model: str
    answers: Mapping[str, Answer]
    usage: TokenUsage
    reported_cost_usd: Decimal | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)


class DecisionClient(Protocol):
    """One interface for decision calls on both providers."""

    async def decide(
        self, request: DecisionRequest, *, agent_id: str | None
    ) -> DecisionResult:
        """Send the request. Charge the cost to ``agent_id``."""


# Request builders


def openai_body(request: DecisionRequest) -> dict[str, Any]:
    """Build the OpenAI Decisions API request body."""
    values = request.values
    if isinstance(values, str):
        input_value: Any = values
    else:
        data = values if isinstance(values, Mapping) else {"values": list(values)}
        input_value = [
            {"role": "user", "content": [{"type": "input_json", "data": data}]}
        ]
    return {
        "model": request.model.model,
        "input": input_value,
        "questions": [_openai_question(q) for q in request.questions],
    }


def _openai_question(q: Question) -> dict[str, Any]:
    if isinstance(q, YesNoQuestion):
        # OpenAI has no field for the yes and no text, so it goes into the
        # instructions.
        text = q.instructions
        if q.yes:
            text += f" Yes means: {q.yes}"
        if q.no:
            text += f" No means: {q.no}"
        return {"type": "predicate", "name": q.name, "instructions": text}
    if isinstance(q, ChoiceQuestion):
        return {
            "type": "choice",
            "name": q.name,
            "instructions": q.instructions,
            "choices": [
                {"value": value, "description": description}
                for value, description in q.options.items()
            ],
        }
    return {
        "type": "score",
        "name": q.name,
        "instructions": q.instructions,
        "levels": [
            {"label": level.label, "description": level.description}
            for level in q.levels
        ],
    }


def openrouter_body(request: DecisionRequest) -> dict[str, Any]:
    """Build the OpenRouter decisions request body."""
    values = request.values
    state = values if isinstance(values, str | Mapping) else list(values)
    return {
        "model": request.model.model,
        "state": state,
        "questions": {q.name: _openrouter_question(q) for q in request.questions},
    }


def _openrouter_question(q: Question) -> dict[str, Any]:
    if isinstance(q, YesNoQuestion):
        out: dict[str, Any] = {"type": "noul", "instructions": q.instructions}
        if q.yes or q.no:
            out["criteria"] = {"true": q.yes or "Yes.", "false": q.no or "No."}
        return out
    if isinstance(q, ChoiceQuestion):
        return {
            "type": "choice",
            "instructions": q.instructions,
            "criteria": dict(q.options),
        }
    return {
        "type": "score",
        "instructions": q.instructions,
        "criteria": [
            f"{level.label}: {level.description}" if level.description else level.label
            for level in q.levels
        ],
    }


# Response parsers


def _float(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        msg = f"decision response: {where} is not a number"
        raise ProviderResponseError(msg)
    return float(value)


def _opt_float(value: Any, where: str) -> float | None:
    return None if value is None else _float(value, where)


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def parse_openai(request: DecisionRequest, body: Mapping[str, Any]) -> DecisionResult:
    """Parse an OpenAI Decisions API response."""
    raw_answers = body.get("answers")
    if not isinstance(raw_answers, list):
        msg = "decision response has no answers list"
        raise ProviderResponseError(msg)
    by_name = {a.get("name"): a for a in raw_answers if isinstance(a, Mapping)}
    answers: dict[str, Answer] = {}
    for q in request.questions:
        answer = by_name.get(q.name)
        if answer is None:
            msg = f"decision response has no answer for {q.name!r}"
            raise ProviderResponseError(msg)
        kind = answer.get("type")
        if kind == "refusal":
            answers[q.name] = Refusal(q.name)
        elif kind == "predicate":
            answers[q.name] = YesNoAnswer(
                q.name, _float(answer.get("probability"), q.name)
            )
        elif kind == "choice":
            answers[q.name] = ChoiceAnswer(
                q.name,
                str(answer.get("choice")),
                {
                    str(p["value"]): _float(p.get("probability"), q.name)
                    for p in answer.get("probabilities") or []
                },
                _opt_float(answer.get("confidence"), q.name),
            )
        elif kind == "score":
            answers[q.name] = ScoreAnswer(
                q.name,
                _float(answer.get("score"), q.name),
                {
                    str(p.get("label", p.get("value"))): _float(
                        p.get("probability"), q.name
                    )
                    for p in answer.get("probabilities") or []
                },
                _opt_float(answer.get("confidence"), q.name),
            )
        else:
            msg = f"decision response has unknown answer type {kind!r}"
            raise ProviderResponseError(msg)
    return DecisionResult(
        model=str(body.get("model", request.model.model)),
        answers=answers,
        usage=openai_usage(body)[0],
        raw=body,
    )


def openai_usage(body: Mapping[str, Any]) -> tuple[TokenUsage, Decimal | None]:
    """Read the token counts of an OpenAI response. OpenAI sends no cost."""
    usage = body.get("usage") or {}
    details = usage.get("input_tokens_details") or {}
    tokens = TokenUsage(
        input_tokens=_int(usage.get("input_tokens")),
        cache_read_tokens=_int(details.get("cached_tokens")),
        cache_write_tokens=_int(details.get("cache_write_tokens")),
        output_tokens=_int(usage.get("output_tokens")),
    )
    return tokens, None


def parse_openrouter(
    request: DecisionRequest, body: Mapping[str, Any]
) -> DecisionResult:
    """Parse an OpenRouter decisions response."""
    raw_answers = body.get("answers")
    if not isinstance(raw_answers, Mapping):
        msg = "decision response has no answers object"
        raise ProviderResponseError(msg)
    answers: dict[str, Answer] = {}
    for q in request.questions:
        answer = raw_answers.get(q.name)
        if not isinstance(answer, Mapping):
            msg = f"decision response has no answer for {q.name!r}"
            raise ProviderResponseError(msg)
        kind = answer.get("type")
        if kind == "refusal":
            answers[q.name] = Refusal(q.name)
        elif kind == "noul":
            answers[q.name] = YesNoAnswer(q.name, _float(answer.get("noul"), q.name))
        elif kind == "choice":
            answers[q.name] = ChoiceAnswer(
                q.name,
                str(answer.get("choice")),
                {
                    str(k): _float(v, q.name)
                    for k, v in (answer.get("probabilities") or {}).items()
                },
                _opt_float(answer.get("confidence"), q.name),
            )
        elif kind == "score" and isinstance(q, ScoreQuestion):
            labels = [level.label for level in q.levels]
            probabilities: dict[str, float] = {}
            for k, v in (answer.get("probabilities") or {}).items():
                index = int(k)
                label = labels[index] if 0 <= index < len(labels) else str(k)
                probabilities[label] = _float(v, q.name)
            answers[q.name] = ScoreAnswer(
                q.name,
                _float(answer.get("score"), q.name),
                probabilities,
                _opt_float(answer.get("confidence"), q.name),
            )
        else:
            msg = f"decision response has unknown answer type {kind!r}"
            raise ProviderResponseError(msg)
    tokens, cost = openrouter_usage(body)
    return DecisionResult(
        model=str(body.get("model", request.model.model)),
        answers=answers,
        usage=tokens,
        reported_cost_usd=cost,
        raw=body,
    )


def openrouter_usage(body: Mapping[str, Any]) -> tuple[TokenUsage, Decimal | None]:
    """Read the token counts and the cost (USD) of an OpenRouter response."""
    usage = body.get("usage") or {}
    cost = usage.get("cost")
    tokens = TokenUsage(
        input_tokens=_int(usage.get("input_tokens")),
        output_tokens=_int(usage.get("output_tokens")),
    )
    return tokens, None if cost is None else Decimal(str(cost))


class DecisionAdapter:
    """Sends decision requests to OpenAI or OpenRouter, with metering."""

    def __init__(self, http: ProviderHTTP, meter: Meter) -> None:
        self._http = http
        self._meter = meter

    async def decide(
        self, request: DecisionRequest, *, agent_id: str | None
    ) -> DecisionResult:
        request.validate()
        provider = request.model.provider
        if provider == "openai":
            body = openai_body(request)
            url = f"{self._http.base_url('openai')}/decisions"
            price_provider = "openai-decisions"
        else:
            body = openrouter_body(request)
            # The decisions route is /api/alpha, next to the /api/v1 base.
            base = self._http.base_url("openrouter").removesuffix("/v1")
            url = f"{base}/alpha/decisions"
            price_provider = "openrouter"
        spec = CallSpec(
            agent_id, provider, "decision", request.model.model, price_provider
        )
        estimate = TokenUsage(input_tokens=len(json.dumps(body)) // CHARS_PER_TOKEN + 1)
        async with self._meter.call(spec, estimate) as call:
            response = await self._http.post(provider, url, body)
            if not isinstance(response, Mapping):
                msg = "decision response is not a JSON object"
                raise ProviderResponseError(msg)
            # Record the usage first: the provider bills the call even when
            # the answers do not parse.
            if provider == "openai":
                call.complete(*openai_usage(response))
                result = parse_openai(request, response)
            else:
                call.complete(*openrouter_usage(response))
                result = parse_openrouter(request, response)
        return result
