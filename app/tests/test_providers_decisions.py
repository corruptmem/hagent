"""Decision adapter tests, with recorded responses from both providers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from hagent_app.providers import (
    ChoiceAnswer,
    ChoiceQuestion,
    DecisionModel,
    DecisionRequest,
    Level,
    ProviderHTTPError,
    ProviderResponseError,
    Refusal,
    ScoreAnswer,
    ScoreQuestion,
    YesNoAnswer,
)
from hagent_app.providers.config import ProvidersConfig
from hagent_app.providers.decisions import (
    decision_models,
    openai_body,
    openrouter_body,
    parse_openai,
)
from helpers.fake_providers import (
    QUESTIONS,
    VALUES,
    Reply,
    fixture,
    openrouter_alpha,
    route,
)
from helpers.providers_env import ProvidersEnv

StartEnv = Callable[..., Awaitable[ProvidersEnv]]

OPENAI = DecisionModel("openai", "gpt-6-luna")
JEV = DecisionModel("openrouter", "typesafe/jev-1.14")


def _request(model: DecisionModel) -> DecisionRequest:
    return DecisionRequest(model, VALUES, QUESTIONS)


def test_openrouter_body_matches_recorded_request() -> None:
    recorded = fixture("decision-openrouter")["request"]
    body = openrouter_body(_request(JEV))
    assert body["questions"] == recorded["questions"]
    assert body["model"] == recorded["model"]


def test_openai_body_has_the_recorded_shape() -> None:
    recorded = fixture("decision-openai")["request"]
    body = openai_body(_request(OPENAI))
    assert [q["type"] for q in body["questions"]] == [
        q["type"] for q in recorded["questions"]
    ]
    assert body["questions"][1]["choices"] == recorded["questions"][1]["choices"]
    assert body["questions"][2]["levels"] == recorded["questions"][2]["levels"]
    # OpenAI has no yes/no fields: the text goes into the instructions.
    assert "Yes means: The room is occupied" in body["questions"][0]["instructions"]
    assert "No means: Heating would waste" in body["questions"][0]["instructions"]
    assert body["input"][0]["content"][0] == {"type": "input_json", "data": VALUES}


def test_openai_text_values_are_sent_as_text() -> None:
    body = openai_body(DecisionRequest(OPENAI, "free text", QUESTIONS[:1]))
    assert body["input"] == "free text"


async def test_openai_decision_answers_and_cost(providers_env: StartEnv) -> None:
    env = await providers_env()
    env.fake.reply(route("openai", "/decisions"), Reply("decision-openai"))
    result = await env.component.decisions.decide(_request(OPENAI), agent_id="a1")

    assert result.answers["should_heat"] == YesNoAnswer("should_heat", 1.0)
    mode = result.answers["mode"]
    assert isinstance(mode, ChoiceAnswer)
    assert mode.choice == "comfort"
    assert mode.probabilities == {"comfort": 1.0, "eco": 0.0, "off": 0.0}
    urgency = result.answers["urgency"]
    assert isinstance(urgency, ScoreAnswer)
    assert urgency.score == pytest.approx(1.09)
    assert urgency.probabilities == {"low": 0.01, "medium": 0.89, "high": 0.1}
    assert urgency.confidence == pytest.approx(0.84)

    sent = env.fake.seen[0]
    assert sent.url == "https://api.openai.test/v1/decisions"
    assert sent.body == openai_body(_request(OPENAI))

    [row] = await env.calls()
    assert row["agent_id"] == "a1"
    assert row["call_type"] == "decision"
    assert row["status"] == "ok"
    assert row["input_tokens"] == 469
    assert row["output_tokens"] == 0
    # OpenAI sends no cost: the price table gives it (USD 0.10 per million).
    assert row["cost_source"] == "price_table"
    assert row["price_source"] == "genai-prices:openai-decisions"
    assert row["cost_pusd"] == 469 * 100_000
    assert row["provider_cost_pusd"] is None
    assert await env.reservations() == 0


async def test_openrouter_decision_answers_cost_and_drift(
    providers_env: StartEnv,
) -> None:
    env = await providers_env()
    env.fake.reply(openrouter_alpha("/decisions"), Reply("decision-openrouter"))
    result = await env.component.decisions.decide(_request(JEV), agent_id=None)

    assert result.answers["should_heat"] == YesNoAnswer("should_heat", 0.95)
    urgency = result.answers["urgency"]
    assert isinstance(urgency, ScoreAnswer)
    # The level numbers map back to the level labels.
    assert urgency.probabilities == {"low": 0, "medium": 0.49, "high": 0.51}
    assert env.fake.seen[0].url == "https://openrouter.test/api/alpha/decisions"

    [row] = await env.calls()
    assert row["agent_id"] is None
    assert row["cost_source"] == "provider"
    assert row["provider_cost_pusd"] == 21_672_000
    assert row["cost_pusd"] == 21_672_000
    # Jev input costs USD 0.042 per million tokens: 516 x 42,000 pico-USD.
    assert row["computed_cost_pusd"] == 516 * 42_000
    assert row["drift"] == pytest.approx(0.0)


async def test_refusal_stays_local_to_its_question(providers_env: StartEnv) -> None:
    env = await providers_env()
    body: dict[str, Any] = fixture("decision-openai")["response"]
    body["answers"][1] = {"type": "refusal", "name": "mode"}
    env.fake.reply(route("openai", "/decisions"), Reply(json_body=body))
    result = await env.component.decisions.decide(_request(OPENAI), agent_id="a1")
    assert result.answers["mode"] == Refusal("mode")
    assert isinstance(result.answers["should_heat"], YesNoAnswer)


async def test_bad_answers_still_record_billed_usage(providers_env: StartEnv) -> None:
    env = await providers_env()
    body: dict[str, Any] = fixture("decision-openrouter")["response"]
    del body["answers"]["mode"]
    env.fake.reply(openrouter_alpha("/decisions"), Reply(json_body=body))
    with pytest.raises(ProviderResponseError, match="mode"):
        await env.component.decisions.decide(_request(JEV), agent_id="a1")
    [row] = await env.calls()
    assert row["status"] == "error"
    assert row["cost_pusd"] == 21_672_000
    assert await env.reservations() == 0


async def test_http_error_records_failed_call(providers_env: StartEnv) -> None:
    env = await providers_env()
    env.fake.reply(
        route("openai", "/decisions"),
        Reply(status=503, json_body={"error": {"message": "overloaded"}}),
    )
    with pytest.raises(ProviderHTTPError) as info:
        await env.component.decisions.decide(_request(OPENAI), agent_id="a1")
    assert info.value.status == 503
    assert info.value.retryable
    [row] = await env.calls()
    assert row["status"] == "error"
    assert row["cost_pusd"] == 0
    assert row["cost_source"] == "none"
    assert await env.reservations() == 0


def test_parse_rejects_unknown_answer_type() -> None:
    body = fixture("decision-openai")["response"]
    body["answers"][0]["type"] = "mystery"
    with pytest.raises(ProviderResponseError, match="mystery"):
        parse_openai(_request(OPENAI), body)


@pytest.mark.parametrize(
    ("questions", "match"),
    [
        ((), "1 to 400"),
        ((QUESTIONS[0], QUESTIONS[0]), "unique"),
        ((ChoiceQuestion("c", "x", {"only": ""}),), "2 to 255"),
        ((ScoreQuestion("s", "x", [Level(str(i)) for i in range(11)]),), "2 to 10"),
    ],
)
def test_request_limits(questions: tuple[Any, ...], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        DecisionRequest(OPENAI, VALUES, questions).validate()


def test_decision_model_list_extends_from_options() -> None:
    config = ProvidersConfig(
        decision_models=[{"provider": "openrouter", "model": "typesafe/jev-2"}]
    )
    models = decision_models(config)
    assert DecisionModel("openai", "gpt-6-luna") in models
    assert models[-1] == DecisionModel("openrouter", "typesafe/jev-2")
    with pytest.raises(ValueError, match="provider"):
        decision_models(ProvidersConfig(decision_models=[{"model": "x"}]))


async def test_openai_decision_cache_tokens_reach_the_record(
    providers_env: StartEnv,
) -> None:
    env = await providers_env()
    body = fixture("decision-openai")["response"]
    body["usage"]["input_tokens"] = 2666
    body["usage"]["input_tokens_details"] = {
        "cached_tokens": 2563,
        "cache_write_tokens": 0,
    }
    env.fake.reply(route("openai", "/decisions"), Reply(json_body=body))
    await env.component.decisions.decide(_request(OPENAI), agent_id="a1")
    [row] = await env.calls()
    assert row["input_tokens"] == 2666
    assert row["cache_read_tokens"] == 2563
