"""The price table.

The price table gives the cost of a call in USD from its token counts. It
has three layers. The first layer that knows the model wins:

1. Overrides from the ``price_overrides`` option.
2. The OpenRouter price lists, fetched once a day (only for OpenRouter).
3. genai-prices, the price library that Pydantic AI uses.

A **price provider** is the provider name that the price layers use. It is
``openai`` for OpenAI chat and embeddings, ``openai-decisions`` for the
OpenAI Decisions API, and ``openrouter`` for all OpenRouter calls.

Token counts follow the Pydantic AI convention: ``input_tokens`` includes
the cache-read and cache-write tokens.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from genai_prices import Usage, calc_price

from hagent_app.providers.errors import PriceUnknownError

MILLION = Decimal(1_000_000)
PICO = Decimal("1e12")


def to_pusd(usd: Decimal | float) -> int:
    """Convert USD to integer pico-USD."""
    return int((Decimal(str(usd)) * PICO).to_integral_value())


def from_pusd(pusd: int) -> Decimal:
    """Convert integer pico-USD to USD."""
    return Decimal(pusd) / PICO


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """The token counts of one call."""

    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True, slots=True)
class Rates:
    """Prices in USD per million tokens.

    A missing cache rate uses the input rate. ``min_input_tokens`` makes the
    rates a tier: they apply when the input has at least that many tokens.
    """

    input: Decimal
    output: Decimal = Decimal(0)
    cache_read: Decimal | None = None
    cache_write: Decimal | None = None
    min_input_tokens: int = 0

    def cost(self, usage: TokenUsage) -> Decimal:
        uncached = max(
            usage.input_tokens - usage.cache_read_tokens - usage.cache_write_tokens, 0
        )
        read = self.input if self.cache_read is None else self.cache_read
        write = self.input if self.cache_write is None else self.cache_write
        total = (
            uncached * self.input
            + usage.cache_read_tokens * read
            + usage.cache_write_tokens * write
            + usage.output_tokens * self.output
        )
        return total / MILLION


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """The rates of one model: a base rate and optional higher tiers."""

    tiers: tuple[Rates, ...]
    dynamic: bool = False

    def cost(self, usage: TokenUsage) -> Decimal:
        rates = self.tiers[0]
        for tier in self.tiers[1:]:
            if usage.input_tokens >= tier.min_input_tokens:
                rates = tier
        return rates.cost(usage)


@dataclass(frozen=True, slots=True)
class PricedCost:
    """A computed cost and the layer that gave the price."""

    usd: Decimal
    source: str
    version: str | None = None


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def rates_from_override(raw: Mapping[str, Any]) -> ModelPrice:
    """Build a ``ModelPrice`` from an override (USD per million tokens)."""
    unknown = set(raw) - {"input", "output", "cache_read", "cache_write"}
    if unknown or "input" not in raw:
        msg = f"price override needs input, output, cache_read, cache_write: {raw}"
        raise ValueError(msg)
    return ModelPrice(
        (
            Rates(
                input=Decimal(str(raw["input"])),
                output=_dec(raw.get("output")) or Decimal(0),
                cache_read=_dec(raw.get("cache_read")),
                cache_write=_dec(raw.get("cache_write")),
            ),
        )
    )


def _per_million(value: Any) -> Decimal | None:
    per_token = _dec(value)
    return None if per_token is None else per_token * MILLION


def rates_from_openrouter(pricing: Mapping[str, Any]) -> ModelPrice:
    """Build a ``ModelPrice`` from an OpenRouter ``pricing`` object.

    OpenRouter gives USD per token as strings. ``-1`` means a dynamic price.
    ``overrides`` holds tiers with ``min_prompt_tokens``.
    """
    base = _openrouter_rates(pricing, 0)
    tiers = [base]
    for tier in pricing.get("overrides") or []:
        merged = {**pricing, **tier}
        tiers.append(_openrouter_rates(merged, int(tier.get("min_prompt_tokens", 0))))
    dynamic = any(t.input < 0 or t.output < 0 for t in tiers)
    return ModelPrice(tuple(tiers), dynamic=dynamic)


def _openrouter_rates(pricing: Mapping[str, Any], min_tokens: int) -> Rates:
    return Rates(
        input=_per_million(pricing.get("prompt")) or Decimal(0),
        output=_per_million(pricing.get("completion")) or Decimal(0),
        cache_read=_per_million(pricing.get("input_cache_read")),
        cache_write=_per_million(pricing.get("input_cache_write")),
        min_input_tokens=min_tokens,
    )


class PriceTable:
    """Computes costs from token counts. See the module docstring."""

    def __init__(
        self,
        overrides: Mapping[str, Mapping[str, Any]] | None = None,
        openrouter: Mapping[str, Mapping[str, Any]] | None = None,
        openrouter_version: str | None = None,
    ) -> None:
        self._overrides = {
            key: rates_from_override(raw) for key, raw in (overrides or {}).items()
        }
        self._openrouter: dict[str, ModelPrice] = {}
        self._openrouter_version: str | None = None
        self.set_openrouter(openrouter or {}, openrouter_version)

    def set_openrouter(
        self, prices: Mapping[str, Mapping[str, Any]], version: str | None
    ) -> None:
        """Replace the OpenRouter layer. ``prices`` maps model to pricing."""
        self._openrouter = {
            model: rates_from_openrouter(pricing) for model, pricing in prices.items()
        }
        self._openrouter_version = version

    @property
    def openrouter_version(self) -> str | None:
        """The fetch time of the OpenRouter layer, or ``None``."""
        return self._openrouter_version

    def cost(self, price_provider: str, model: str, usage: TokenUsage) -> PricedCost:
        """Compute the cost of ``usage``. Raise ``PriceUnknownError``."""
        override = self._overrides.get(f"{price_provider}:{model}")
        if override is not None:
            return PricedCost(override.cost(usage), "override")
        if price_provider == "openrouter" and model in self._openrouter:
            price = self._openrouter[model]
            if price.dynamic:
                msg = f"OpenRouter lists a dynamic price for {model}"
                raise PriceUnknownError(msg)
            return PricedCost(price.cost(usage), "openrouter", self._openrouter_version)
        return self._genai_prices(price_provider, model, usage)

    def _genai_prices(
        self, price_provider: str, model: str, usage: TokenUsage
    ) -> PricedCost:
        candidates = [(price_provider, model)]
        if price_provider == "openrouter" and "/" in model:
            # OpenRouter IDs are "<vendor>/<model>". genai-prices often knows
            # the model under the vendor, for example openai embeddings.
            vendor, _, name = model.partition("/")
            candidates.append((vendor, name))
        genai_usage = Usage(
            input_tokens=usage.input_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            output_tokens=usage.output_tokens,
        )
        for provider_id, model_ref in candidates:
            try:
                result = calc_price(
                    genai_usage, model_ref=model_ref, provider_id=provider_id
                )
            except LookupError:
                continue
            version = (
                result.auto_update_timestamp.isoformat()
                if result.auto_update_timestamp
                else "snapshot"
            )
            return PricedCost(
                Decimal(result.total_price), f"genai-prices:{provider_id}", version
            )
        msg = f"no price for {price_provider}:{model}"
        raise PriceUnknownError(msg)


def load_openrouter_rows(rows: list[tuple[str, str, str]]) -> dict[str, Any]:
    """Turn ``(model, pricing JSON, fetched_at)`` rows into a price mapping."""
    return {model: json.loads(pricing) for model, pricing, _ in rows}
