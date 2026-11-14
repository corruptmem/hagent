"""Chat adapter tests: Pydantic AI models with recorded responses."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest
from hagent_app.providers import BudgetExceededError, MeteredModel
from helpers.fake_providers import Reply, route
from helpers.providers_env import ProvidersEnv
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.models.openrouter import OpenRouterModel

StartEnv = Callable[..., Awaitable[ProvidersEnv]]

CASES = [
    ("openai", "gpt-4o-mini", "/responses", "chat-openai"),
    ("openrouter", "openai/gpt-4o-mini", "/chat/completions", "chat-openrouter"),
]


async def _run(model: MeteredModel, *, stream: bool) -> str:
    agent = Agent(model, instructions="Answer in one word.")
    if stream:
        async with agent.run_stream("Name a primary colour.") as result:
            return await result.get_output()
    run = await agent.run("Name a primary colour.")
    return run.output


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(("provider", "model", "path", "name"), CASES)
async def test_chat_on_both_providers(  # noqa: PLR0913, PLR0917 - parametrized
    providers_env: StartEnv,
    provider: str,
    model: str,
    path: str,
    name: str,
    stream: bool,
) -> None:
    env = await providers_env()
    fixture_name = f"{name}-stream" if stream else name
    env.fake.reply(route(provider, path), Reply(fixture_name))
    metered = env.component.chat_model(provider, model, agent_id="a1")  # type: ignore[arg-type]
    expected = OpenAIResponsesModel if provider == "openai" else OpenRouterModel
    assert isinstance(metered.wrapped, expected)

    output = await _run(metered, stream=stream)
    assert output.startswith("Blue")
    assert env.fake.seen[0].url.endswith(path)

    [row] = await env.calls()
    assert row["call_type"] == "chat"
    assert row["agent_id"] == "a1"
    assert row["status"] == "ok"
    assert row["input_tokens"] == 21
    assert row["reserved_pusd"] > row["cost_pusd"] > 0
    if provider == "openai":
        assert row["cost_source"] == "price_table"
        # gpt-4o-mini: USD 0.15 per million input, 0.60 per million output.
        assert row["cost_pusd"] == 21 * 150_000 + 3 * 600_000
    else:
        assert row["cost_source"] == "provider"
        reported = 4_306_500 if not stream else 3_712_500
        assert row["provider_cost_pusd"] == reported
        # OpenRouter bills 0.99 x the listed price (task-3).
        assert row["drift"] == pytest.approx(-0.01)
    assert await env.reservations() == 0


async def test_shared_http_client_survives_runs(providers_env: StartEnv) -> None:
    env = await providers_env()
    env.fake.reply(route("openai", "/responses"), Reply("chat-openai"))
    for _ in range(2):
        model = env.component.chat_model("openai", "gpt-4o-mini", agent_id="a1")
        assert await _run(model, stream=False) == "Blue."
    assert len(await env.calls()) == 2


async def test_reservation_uses_max_tokens(providers_env: StartEnv) -> None:
    # USD 0.001 is too small for the default output limit (4,096 tokens x
    # USD 0.60 per million), so the request fails before any HTTP call.
    env = await providers_env(agent_daily_budget_usd=0.001)
    env.fake.reply(route("openai", "/responses"), Reply("chat-openai"))
    model = env.component.chat_model("openai", "gpt-4o-mini", agent_id="a1")
    with pytest.raises(BudgetExceededError):
        await _run(model, stream=False)
    assert env.fake.seen == []
    # A small max_tokens setting fits in the same budget.
    agent = Agent(model, instructions="Answer in one word.")
    run = await agent.run("Name a primary colour.", model_settings={"max_tokens": 1})
    assert run.output == "Blue."
