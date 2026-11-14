"""The providers component.

``ProvidersComponent`` builds the price table, the meter and the adapters on
start. Other components use it for model calls:

- ``chat_model(provider, model, agent_id=...)`` returns a metered Pydantic AI
  chat model.
- ``decisions.decide(request, agent_id=...)`` sends a decision request.
- ``embeddings.embed(provider, model, texts, agent_id=...)`` creates
  embeddings.
- ``meter.budget_state(agent_id)`` gives the spend and budget of today.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx2

from hagent_app.providers.chat import MeteredModel, build_chat_model
from hagent_app.providers.config import (
    ConfigKeySource,
    KeySource,
    Provider,
    ProvidersConfig,
)
from hagent_app.providers.decisions import (
    DecisionAdapter,
    DecisionModel,
    decision_models,
)
from hagent_app.providers.embeddings import EmbeddingAdapter
from hagent_app.providers.http import DEFAULT_TIMEOUT, ProviderHTTP
from hagent_app.providers.meter import Clock, Meter, local_now
from hagent_app.providers.openrouter_prices import (
    FIRST_CHECK_DELAY,
    OpenRouterPriceRefresher,
    load_stored,
)
from hagent_app.providers.prices import PriceTable

if TYPE_CHECKING:
    from hagent_app.app import AppContext

_LOG = logging.getLogger(__name__)


class NotStartedError(RuntimeError):
    """The component is not started."""


class ProvidersComponent:
    """Provider adapters, price table and budgets."""

    name = "providers"

    def __init__(
        self,
        *,
        http_client: httpx2.AsyncClient | None = None,
        keys: KeySource | None = None,
        clock: Clock = local_now,
        refresh_delay: float = FIRST_CHECK_DELAY,
    ) -> None:
        self._external_client = http_client
        self._keys_override = keys
        self._clock = clock
        self._refresh_delay = refresh_delay
        self._client: httpx2.AsyncClient | None = None
        self._refresher: OpenRouterPriceRefresher | None = None
        self._config: ProvidersConfig | None = None
        self._meter: Meter | None = None
        self._decisions: DecisionAdapter | None = None
        self._embeddings: EmbeddingAdapter | None = None
        self._keys: KeySource | None = None
        self.decision_models: list[DecisionModel] = []

    async def start(self, ctx: AppContext) -> None:
        config = ctx.config.section("providers", ProvidersConfig)
        self._config = config
        self.decision_models = decision_models(config)
        self._keys = self._keys_override or ConfigKeySource(config)
        stored, version = await load_stored(ctx.db)
        prices = PriceTable(config.price_overrides, stored, version)
        self._meter = Meter(ctx.db, config, prices, self._keys, self._clock)
        cleared = await self._meter.clear_reservations()
        if cleared:
            _LOG.info("cleared %d budget reservations from the last run", cleared)
        self._client = self._external_client or httpx2.AsyncClient(
            timeout=DEFAULT_TIMEOUT
        )
        http = ProviderHTTP(config, self._keys, self._client)
        self._decisions = DecisionAdapter(http, self._meter)
        self._embeddings = EmbeddingAdapter(http, self._meter)
        if config.refresh_openrouter_prices:
            self._refresher = OpenRouterPriceRefresher(
                ctx.db,
                prices,
                config.base_url("openrouter"),
                self._client,
                clock=self._clock,
                first_delay=self._refresh_delay,
            )
            self._refresher.start()

    async def stop(self) -> None:
        if self._refresher is not None:
            await self._refresher.stop()
            self._refresher = None
        if self._client is not None and self._external_client is None:
            await self._client.aclose()
        self._client = None

    @property
    def meter(self) -> Meter:
        if self._meter is None:
            raise NotStartedError(self.name)
        return self._meter

    @property
    def decisions(self) -> DecisionAdapter:
        if self._decisions is None:
            raise NotStartedError(self.name)
        return self._decisions

    @property
    def embeddings(self) -> EmbeddingAdapter:
        if self._embeddings is None:
            raise NotStartedError(self.name)
        return self._embeddings

    def chat_model(
        self, provider: Provider, model: str, *, agent_id: str | None
    ) -> MeteredModel:
        """A metered chat model. Charge its cost to ``agent_id``."""
        if self._config is None or self._keys is None or self._client is None:
            raise NotStartedError(self.name)
        inner = build_chat_model(
            provider, model, self._keys, self._config, self._client
        )
        return MeteredModel(
            inner,
            self.meter,
            provider=provider,
            agent_id=agent_id,
            max_output_tokens=self._config.chat_max_output_tokens,
        )
