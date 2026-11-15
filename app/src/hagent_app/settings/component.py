"""The settings component.

``SettingsComponent`` owns the settings store and its management routes
(``api.py``). Put it before ``ProvidersComponent`` in
``default_components()``: the providers read their keys and budgets from it
when they start.

Other areas use three things:

- ``keys``: the ``KeySource`` of the providers (``keys.py``).
- ``providers_config(base)``: ``base`` with the user's budgets, price
  overrides, auto-update choice and decision models in place of the option
  values.
- ``approvals()``: the approval settings, or ``None``.

``subscribe(fn)`` calls ``fn`` after each change.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from aiohttp import web

from hagent_app.providers.config import ProvidersConfig
from hagent_app.settings.api import SettingsApi
from hagent_app.settings.keys import SettingsKeySource
from hagent_app.settings.model import Approvals
from hagent_app.settings.service import Listener, SettingsService

if TYPE_CHECKING:
    from hagent_app.app import AppContext
    from hagent_app.http.events import ChangeEvents


class SettingsNotStartedError(RuntimeError):
    """The settings component is not started."""


class SettingsComponent:
    """The settings store and its routes. See the module docstring."""

    name = "settings"

    def __init__(
        self,
        events: ChangeEvents | None = None,
        *,
        env: dict[str, str] | None = None,
    ) -> None:
        self._events = events
        self._env = env
        self._service: SettingsService | None = None
        self._keys: SettingsKeySource | None = None
        self._base: ProvidersConfig | None = None
        self._pending: list[Listener] = []

    def routes(self) -> list[web.RouteDef]:
        """The routes under ``/api/settings``."""
        return SettingsApi(self).routes()

    async def start(self, ctx: AppContext) -> None:
        self._base = ctx.config.section("providers", ProvidersConfig)
        service = SettingsService(ctx.db)
        await service.load()
        for listener in self._pending:
            service.subscribe(listener)
        self._pending.clear()
        self._keys = SettingsKeySource(service, self._base, self._env)
        self._service = service

    async def stop(self) -> None:
        self._service = None

    def service(self) -> SettingsService:
        if self._service is None:
            raise SettingsNotStartedError(self.name)
        return self._service

    @property
    def keys(self) -> SettingsKeySource:
        if self._keys is None:
            raise SettingsNotStartedError(self.name)
        return self._keys

    @property
    def base(self) -> ProvidersConfig:
        if self._base is None:
            raise SettingsNotStartedError(self.name)
        return self._base

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        """Call ``listener`` with the changed names after each change."""
        if self._service is None:
            self._pending.append(listener)
            return lambda: self._pending.remove(listener)
        return self._service.subscribe(listener)

    def approvals(self) -> Approvals | None:
        """The approval settings, or ``None`` when the user set none."""
        return self.service().approvals()

    def providers_config(self, base: ProvidersConfig | None = None) -> ProvidersConfig:
        """``base`` (the option section) with the stored settings applied."""
        base = base or self.base
        service = self.service()
        changes: dict[str, Any] = {}
        for name in (
            "installation_daily_budget_usd",
            "agent_daily_budget_usd",
            "price_overrides",
            "genai_prices_auto_update",
            "decision_models",
        ):
            value = service.stored(name)
            if value is not None:
                changes[name] = value
        return dataclasses.replace(base, **changes)

    def publish_change(self) -> None:
        """Send the ``settings`` / ``updated`` change event (no data)."""
        if self._events is not None:
            self._events.publish("settings", "updated", "settings")
