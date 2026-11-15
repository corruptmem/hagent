"""The providers component.

``ProvidersComponent`` builds the price table, the meter and the adapters on
start. Other components use it for model calls:

- ``chat_model(provider, model, agent_id=...)`` returns a metered Pydantic AI
  chat model.
- ``decisions.decide(request, agent_id=...)`` sends a decision request.
- ``embeddings.embed(provider, model, texts, agent_id=...)`` creates
  embeddings.
- ``meter.budget_state(agent_id)`` gives the spend and budget of today.

With a ``SettingsComponent`` (task-20), the component reads the API keys
through the settings key source, and the budgets, price overrides,
decision models and the genai-prices auto-update from the settings. A
settings change takes effect without a restart.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

import httpx2
from genai_prices import UpdatePrices

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
    from hagent_app.settings import SettingsComponent

_LOG = logging.getLogger(__name__)


class NotStartedError(RuntimeError):
    """The component is not started."""


class PriceUpdater(Protocol):
    """The genai-prices background update (``UpdatePrices``)."""

    def start(self) -> object: ...

    def stop(self) -> object: ...


class ProvidersComponent:
    """Provider adapters, price table and budgets."""

    name = "providers"

    def __init__(  # noqa: PLR0913 - keyword-only services
        self,
        *,
        http_client: httpx2.AsyncClient | None = None,
        keys: KeySource | None = None,
        clock: Clock = local_now,
        refresh_delay: float = FIRST_CHECK_DELAY,
        settings: SettingsComponent | None = None,
        price_updater: Callable[[], PriceUpdater] = UpdatePrices,
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
        self._settings = settings
        self._make_updater = price_updater
        self._updater: PriceUpdater | None = None
        self._prices: PriceTable | None = None
        self._base: ProvidersConfig | None = None
        self._unsubscribe: Callable[[], None] | None = None

    async def start(self, ctx: AppContext) -> None:
        base = ctx.config.section("providers", ProvidersConfig)
        self._base = base
        settings = self._settings
        config = settings.providers_config(base) if settings is not None else base
        self._config = config
        self.decision_models = decision_models(config)
        if self._keys_override is not None:
            self._keys = self._keys_override
        elif settings is not None:
            self._keys = settings.keys
        else:
            self._keys = ConfigKeySource(base)
        stored, version = await load_stored(ctx.db)
        prices = PriceTable(config.price_overrides, stored, version)
        self._prices = prices
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
        self._set_auto_update(config.genai_prices_auto_update)
        if settings is not None:
            self._unsubscribe = settings.subscribe(self._on_settings)

    def _on_settings(self, _names: frozenset[str]) -> None:
        """Apply the stored settings to the budgets, prices and models."""
        if self._settings is None or self._base is None or self._meter is None:
            return
        config = self._settings.providers_config(self._base)
        self._config = config
        self._meter.set_config(config)
        if self._prices is not None:
            self._prices.set_overrides(config.price_overrides)
        self.decision_models = decision_models(config)
        self._set_auto_update(config.genai_prices_auto_update)

    def _set_auto_update(self, enabled: bool) -> None:
        """Start or stop the genai-prices background update."""
        if enabled and self._updater is None:
            self._updater = self._make_updater()
            self._updater.start()
            _LOG.info("genai-prices auto-update is on")
        elif not enabled and self._updater is not None:
            self._updater.stop()
            self._updater = None
            _LOG.info("genai-prices auto-update is off")

    async def stop(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        self._set_auto_update(False)
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
