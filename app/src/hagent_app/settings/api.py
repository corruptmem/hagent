"""The management routes for settings. ``docs/api.md`` describes them.

No response, error text or change event holds an API key. A key appears
only as a hint (see ``keys.py``).
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any, cast

from aiohttp import web
from pydantic import ValidationError

from hagent_app.http.errors import ApiError, read_json
from hagent_app.providers.config import PROVIDERS, Provider
from hagent_app.providers.decisions import DEFAULT_DECISION_MODELS
from hagent_app.settings.keys import KeyFormatError, check_key, hint
from hagent_app.settings.model import parse_patch, validation_details

if TYPE_CHECKING:
    from hagent_app.settings.component import SettingsComponent

_BASE = "/api/settings"
_KEY = _BASE + "/keys/{provider:openai|openrouter}"


class SettingsApi:
    """The routes of the settings area."""

    def __init__(self, component: SettingsComponent) -> None:
        self._component = component

    def routes(self) -> list[web.RouteDef]:
        return [
            web.get(_BASE, self.get_settings),
            web.patch(_BASE, self.patch_settings),
            web.put(_KEY, self.put_key),
            web.delete(_KEY, self.delete_key),
        ]

    def _body(self) -> dict[str, Any]:
        component = self._component
        try:
            service = component.service()
            keys = component.keys
        except RuntimeError as err:
            raise ApiError(503, "not_ready", "the settings are not loaded") from err
        base = component.base
        key_info = {}
        for provider in PROVIDERS:
            source, secret = keys.lookup(provider)
            key_info[provider] = {
                "set": bool(secret),
                "hint": hint(secret),
                "source": source,
            }
        return {
            "keys": key_info,
            "settings": service.stored_values(),
            "defaults": {
                "installation_daily_budget_usd": base.installation_daily_budget_usd,
                "agent_daily_budget_usd": base.agent_daily_budget_usd,
                "price_overrides": dict(base.price_overrides),
                "genai_prices_auto_update": base.genai_prices_auto_update,
                "decision_models": [dict(m) for m in base.decision_models],
                "approvals": None,
            },
            "built_in_decision_models": [
                dataclasses.asdict(m) for m in DEFAULT_DECISION_MODELS
            ],
        }

    async def get_settings(self, request: web.Request) -> web.Response:
        return web.json_response(self._body())

    async def patch_settings(self, request: web.Request) -> web.Response:
        self._body()  # 503 before start
        raw = await read_json(request)
        if not isinstance(raw, dict):
            raise ApiError(400, "invalid_body", "the body must be a JSON object")
        try:
            patch = parse_patch(raw)
        except ValidationError as err:
            raise ApiError(
                422,
                "settings_invalid",
                "the settings are not valid",
                validation_details(err),
            ) from err
        changes = patch.changes()
        if changes:
            await self._component.service().update(changes)
            self._component.publish_change()
        return web.json_response(self._body())

    async def put_key(self, request: web.Request) -> web.Response:
        self._body()
        provider = cast("Provider", request.match_info["provider"])
        raw = await read_json(request)
        if not isinstance(raw, dict) or set(raw) != {"key"}:
            raise ApiError(400, "invalid_body", 'the body must be {"key": "<API key>"}')
        try:
            value = check_key(raw["key"])
        except KeyFormatError as err:
            raise ApiError(422, "invalid_key", str(err)) from None
        await self._component.service().set_key(provider, value)
        self._component.publish_change()
        return web.json_response(self._body())

    async def delete_key(self, request: web.Request) -> web.Response:
        self._body()
        provider = cast("Provider", request.match_info["provider"])
        await self._component.service().set_key(provider, None)
        self._component.publish_change()
        return web.json_response(self._body())
