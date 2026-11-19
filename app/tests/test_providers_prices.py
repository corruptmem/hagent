"""Price table and OpenRouter price refresh tests."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx2
import pytest
from hagent_app.providers import PriceTable, PriceUnknownError, TokenUsage
from hagent_app.providers.openrouter_prices import OpenRouterPriceRefresher
from hagent_app.providers.prices import from_pusd, to_pusd
from helpers.fake_providers import OPENROUTER_BASE, OPENROUTER_KEY, Reply, route
from helpers.providers_env import ProvidersEnv

StartEnv = Callable[..., Awaitable[ProvidersEnv]]

TIERED = {
    "prompt": "0.000001",
    "completion": "0.000005",
    "input_cache_read": "0.00000005",
    "input_cache_write": "0.00000125",
    "overrides": [
        {
            "min_prompt_tokens": 272000,
            "prompt": "0.000002",
            "completion": "0.0000075",
            "input_cache_read": "0.0000001",
            "input_cache_write": "0.0000025",
        }
    ],
}


def test_pico_usd_round_trip() -> None:
    assert to_pusd(Decimal("0.000000042")) == 42_000
    assert to_pusd(0.5) == 500_000_000_000
    assert from_pusd(42_000) == Decimal("0.000000042")


def test_genai_prices_layer_with_cache_tokens() -> None:
    table = PriceTable()
    # Observed upstream cost of a cached gpt-4o-mini call (task-3).
    usage = TokenUsage(input_tokens=2571, cache_read_tokens=2432, output_tokens=1)
    cost = table.cost("openai", "gpt-4o-mini", usage)
    assert cost.usd == Decimal("0.00020385")
    assert cost.source == "genai-prices:openai"
    assert cost.version == "snapshot"


def test_openrouter_vendor_fallback_to_genai_prices() -> None:
    table = PriceTable()
    cost = table.cost(
        "openrouter", "openai/text-embedding-3-small", TokenUsage(input_tokens=14)
    )
    assert cost.usd == Decimal("2.8e-7")
    assert cost.source == "genai-prices:openai"


def test_override_wins_over_every_layer() -> None:
    table = PriceTable(
        overrides={"openai-decisions:gpt-6-luna": {"input": 0.1, "cache_read": 0.01}},
        openrouter={"openai/gpt-6-luna": TIERED},
    )
    usage = TokenUsage(input_tokens=2000, cache_read_tokens=1000)
    cost = table.cost("openai-decisions", "gpt-6-luna", usage)
    assert cost.source == "override"
    assert cost.usd == Decimal("0.00011")
    with pytest.raises(ValueError, match="price override"):
        PriceTable(overrides={"openai:x": {"inputs": 1}})


def test_openrouter_layer_tiers_and_cache_rates() -> None:
    table = PriceTable(openrouter={"vendor/tiered": TIERED}, openrouter_version="v1")
    small = TokenUsage(input_tokens=1000, cache_write_tokens=600, output_tokens=10)
    cost = table.cost("openrouter", "vendor/tiered", small)
    assert cost.source == "openrouter"
    assert cost.version == "v1"
    assert cost.usd == Decimal("0.0004") + Decimal("0.00075") + Decimal("0.00005")
    big = TokenUsage(input_tokens=300_000)
    assert table.cost("openrouter", "vendor/tiered", big).usd == Decimal("0.6")


def test_dynamic_and_unknown_prices_raise() -> None:
    table = PriceTable(
        openrouter={"typesafe/jev-router": {"prompt": "-1", "completion": "-1"}}
    )
    with pytest.raises(PriceUnknownError, match="dynamic"):
        table.cost("openrouter", "typesafe/jev-router", TokenUsage(input_tokens=1))
    with pytest.raises(PriceUnknownError, match="no price"):
        table.cost("openai", "no-such-model", TokenUsage(input_tokens=1))


def _models(entries: dict[str, Any]) -> dict[str, Any]:
    return {"data": [{"id": k, "pricing": v} for k, v in entries.items()]}


async def test_refresh_stores_prices_and_updates_table(
    providers_env: StartEnv,
) -> None:
    env = await providers_env()
    env.fake.reply(
        route("openrouter", "/models"),
        Reply(json_body=_models({"vendor/tiered": TIERED})),
    )
    env.fake.reply(
        route("openrouter", "/embeddings/models"),
        Reply(json_body=_models({"vendor/embed": {"prompt": "0.00000002"}})),
    )
    now = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
    clock_value = [now]
    prices = env.component.meter.prices
    async with env.fake.client() as client:
        refresher = OpenRouterPriceRefresher(
            env.db, prices, OPENROUTER_BASE, client, clock=lambda: clock_value[0]
        )
        assert await refresher.refresh_if_stale() is True
        assert prices.openrouter_version == now.isoformat()
        cost = prices.cost(
            "openrouter", "vendor/embed", TokenUsage(input_tokens=1_000_000)
        )
        assert cost.usd == Decimal("0.02")
        assert cost.source == "openrouter"
        # Within one day the stored copy is fresh.
        clock_value[0] = now + timedelta(hours=23)
        assert await refresher.refresh_if_stale() is False
        clock_value[0] = now + timedelta(hours=25)
        assert await refresher.refresh_if_stale() is True
    # The price lists are public: the refresh sends no API key.
    assert len(env.fake.seen) == 4
    for seen in env.fake.seen:
        assert "authorization" not in seen.headers
        assert OPENROUTER_KEY not in str(seen.headers)


async def test_refresh_rejects_empty_lists(providers_env: StartEnv) -> None:
    env = await providers_env()
    env.fake.reply(route("openrouter", "/models"), Reply(json_body={"data": []}))
    env.fake.reply(
        route("openrouter", "/embeddings/models"), Reply(json_body={"data": []})
    )
    async with env.fake.client() as client:
        refresher = OpenRouterPriceRefresher(
            env.db, env.component.meter.prices, OPENROUTER_BASE, client
        )
        with pytest.raises(ValueError, match="empty"):
            await refresher.refresh()


async def test_refresh_http_error_raises(providers_env: StartEnv) -> None:
    env = await providers_env()
    env.fake.reply(route("openrouter", "/models"), Reply(status=500, json_body={}))
    async with env.fake.client() as client:
        refresher = OpenRouterPriceRefresher(
            env.db, env.component.meter.prices, OPENROUTER_BASE, client
        )
        with pytest.raises(httpx2.HTTPStatusError):
            await refresher.refresh()
