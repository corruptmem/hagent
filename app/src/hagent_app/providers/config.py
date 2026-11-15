"""Configuration and API keys of the providers area.

The user enters the keys in the management UI settings (task-20,
``hagent_app.settings``). The ``providers`` section of ``options.json`` and
the environment are fallbacks. Code that needs a key asks a ``KeySource``,
so that the source can change without a change to the adapters.

A ``Secret`` holds a key. Its ``repr`` and ``str`` never show the value.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

Provider = Literal["openai", "openrouter"]
PROVIDERS: tuple[Provider, ...] = ("openai", "openrouter")

DEFAULT_BASE_URLS: Mapping[Provider, str] = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
}
KEY_ENV = {"openai": "HAGENT_OPENAI_API_KEY", "openrouter": "HAGENT_OPENROUTER_API_KEY"}


class Secret:
    """A secret string. ``repr`` and ``str`` hide the value."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        """Return the value. Give it only to the HTTP client."""
        return self._value

    def __repr__(self) -> str:
        return "Secret('***')"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and other._value == self._value

    def __hash__(self) -> int:
        return hash(self._value)

    def __bool__(self) -> bool:
        return bool(self._value)


@dataclass(frozen=True)
class ProvidersConfig:
    """The ``providers`` section of ``options.json``.

    - ``openai_api_key``, ``openrouter_api_key``: the API keys.
    - ``base_urls``: replace the API base URL of a provider (for tests or a
      proxy).
    - ``decision_models``: extra decision models, as ``{"model": ...,
      "provider": ...}`` objects. They extend ``DEFAULT_DECISION_MODELS``.
    - ``price_overrides``: prices in USD per million tokens, keyed by
      ``"<price provider>:<model>"``. Each value has ``input`` and
      ``output``, and can have ``cache_read`` and ``cache_write``.
    - ``agent_daily_budget_usd``: the default daily budget of each agent.
    - ``agent_budgets_usd``: daily budgets for single agents, keyed by agent
      ID. They apply only when the definition of the agent has
      ``budget.daily_usd`` set to ``null`` (task-20).
    - ``installation_daily_budget_usd``: the daily budget of the
      installation. It counts the costs of all agents and the costs that
      belong to no agent.
    - ``unknown_price_reserve_usd``: the amount to reserve for a call when no
      price is known. ``None`` refuses such calls.
    - ``chat_max_output_tokens``: the output token limit that the chat
      reservation uses when the model settings set no ``max_tokens``.
    - ``drift_warn_ratio``: log a warning when the reported cost and the
      computed cost differ by more than this ratio.
    - ``refresh_openrouter_prices``: fetch the OpenRouter price lists once a
      day.
    - ``genai_prices_auto_update``: let genai-prices fetch its newest price
      data from GitHub once an hour. Off by default.

    The settings store (task-20) replaces the budgets, the price overrides,
    the decision models and ``genai_prices_auto_update`` with the values
    that the user sets in the management UI.
    """

    openai_api_key: str = field(default="", repr=False)
    openrouter_api_key: str = field(default="", repr=False)
    base_urls: Mapping[str, str] = field(default_factory=dict)
    decision_models: list[Mapping[str, Any]] = field(default_factory=list)
    price_overrides: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    agent_daily_budget_usd: float = 0.50
    agent_budgets_usd: Mapping[str, float] = field(default_factory=dict)
    installation_daily_budget_usd: float = 5.00
    unknown_price_reserve_usd: float | None = None
    chat_max_output_tokens: int = 4096
    drift_warn_ratio: float = 0.05
    refresh_openrouter_prices: bool = True
    genai_prices_auto_update: bool = False

    def base_url(self, provider: Provider) -> str:
        """The API base URL of ``provider``, without a trailing slash."""
        return self.base_urls.get(provider, DEFAULT_BASE_URLS[provider]).rstrip("/")


class KeySource(Protocol):
    """Gives the API key of a provider."""

    def key(self, provider: Provider) -> Secret | None:
        """Return the key, or ``None`` when no key is set."""


class ConfigKeySource:
    """Reads the keys from ``ProvidersConfig`` and the environment.

    ``HAGENT_OPENAI_API_KEY`` and ``HAGENT_OPENROUTER_API_KEY`` override the
    options, for development.
    """

    def __init__(
        self, config: ProvidersConfig, env: Mapping[str, str] | None = None
    ) -> None:
        env = os.environ if env is None else env
        self._keys: dict[str, Secret] = {}
        for provider, configured in (
            ("openai", config.openai_api_key),
            ("openrouter", config.openrouter_api_key),
        ):
            value = env.get(KEY_ENV[provider]) or configured
            if value:
                self._keys[provider] = Secret(value)

    def key(self, provider: Provider) -> Secret | None:
        return self._keys.get(provider)

    def __repr__(self) -> str:
        return f"ConfigKeySource(providers={sorted(self._keys)})"


def redact(text: str, keys: KeySource) -> str:
    """Replace each known API key in ``text`` with ``***``."""
    for provider in PROVIDERS:
        secret = keys.key(provider)
        if secret:
            text = text.replace(secret.reveal(), "***")
    return text
