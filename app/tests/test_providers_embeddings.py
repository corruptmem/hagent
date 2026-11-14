"""Embedding adapter tests, with recorded responses from both providers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest
from hagent_app.providers import ProviderResponseError
from helpers.fake_providers import Reply, fixture, route
from helpers.providers_env import ProvidersEnv

StartEnv = Callable[..., Awaitable[ProvidersEnv]]
TEXTS = ["Turn on the heating.", "The light is off."]


@pytest.mark.parametrize(
    ("provider", "model", "name"),
    [
        ("openai", "text-embedding-3-small", "embeddings-openai"),
        ("openrouter", "openai/text-embedding-3-small", "embeddings-openrouter"),
    ],
)
async def test_embeddings_on_both_providers(
    providers_env: StartEnv, provider: str, model: str, name: str
) -> None:
    env = await providers_env()
    env.fake.reply(route(provider, "/embeddings"), Reply(name))
    result = await env.component.embeddings.embed(
        provider,  # type: ignore[arg-type]
        model,
        TEXTS,
        agent_id="a1",
        dimensions=8,
    )
    recorded = fixture(name)
    assert env.fake.seen[0].body == recorded["request"]
    assert len(result.vectors) == 2
    assert all(len(v) == 8 for v in result.vectors)
    assert result.vectors[0] == recorded["response"]["data"][0]["embedding"]
    assert result.usage.input_tokens == 10

    [row] = await env.calls()
    assert row["call_type"] == "embedding"
    assert row["input_tokens"] == 10
    # text-embedding-3-small costs USD 0.02 per million tokens.
    assert row["computed_cost_pusd"] == 10 * 20_000
    if provider == "openrouter":
        assert row["cost_source"] == "provider"
        assert row["provider_cost_pusd"] == 200_000
        assert row["price_source"] == "genai-prices:openai"
    else:
        assert row["cost_source"] == "price_table"
        assert row["provider_cost_pusd"] is None


async def test_empty_batch_makes_no_call(providers_env: StartEnv) -> None:
    env = await providers_env()
    result = await env.component.embeddings.embed(
        "openai", "text-embedding-3-small", [], agent_id=None
    )
    assert result.vectors == []
    assert env.fake.seen == []


async def test_vector_count_mismatch_is_an_error(providers_env: StartEnv) -> None:
    env = await providers_env()
    body = fixture("embeddings-openai")["response"]
    body["data"] = body["data"][:1]
    env.fake.reply(route("openai", "/embeddings"), Reply(json_body=body))
    with pytest.raises(ProviderResponseError, match="2 vectors"):
        await env.component.embeddings.embed(
            "openai", "text-embedding-3-small", TEXTS, agent_id=None
        )
    [row] = await env.calls()
    assert row["status"] == "error"
    assert row["cost_source"] == "price_table"
