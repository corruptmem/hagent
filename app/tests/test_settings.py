"""The settings store and its routes (task-20, design section 7.2).

The API keys never appear in a response, an error, a change event or a log
line. The providers read keys, budgets, price overrides and decision
models from the settings, and a change takes effect without a restart.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer
from hagent_app.agents import AgentDefinition, AgentService
from hagent_app.agents.definition import ModelRef, Models
from hagent_app.app import AppContext
from hagent_app.config import Config
from hagent_app.ha import HAClient, StateCache
from hagent_app.http import ChangeEvent, ChangeEvents, HttpServer
from hagent_app.providers import (
    BudgetExceededError,
    CallSpec,
    DecisionModel,
    DecisionRequest,
    PriceUnknownError,
    ProviderHTTPError,
    ProvidersComponent,
)
from hagent_app.providers.decisions import DEFAULT_DECISION_MODELS
from hagent_app.providers.prices import TokenUsage
from hagent_app.settings import SettingsComponent
from hagent_app.storage import Database
from helpers.fake_providers import (
    OPENAI_KEY,
    QUESTIONS,
    VALUES,
    FakeProviders,
    Reply,
    provider_options,
    route,
)

NEW_KEY = "sk-test-settings-SENTINEL-0003-abcd"
APPROVER = "c" * 32


class FakeUpdater:
    """Stands in for genai-prices ``UpdatePrices``. It never fetches."""

    instances: list[FakeUpdater] = []  # noqa: RUF012 - test registry

    def __init__(self) -> None:
        self.running = False
        FakeUpdater.instances.append(self)

    def start(self) -> None:
        self.running = True

    def stop(self) -> None:
        self.running = False


@dataclass
class Env:
    settings: SettingsComponent
    providers: ProvidersComponent
    client: TestClient[Any, Any]
    fake: FakeProviders
    events: list[ChangeEvent] = field(default_factory=list)

    async def request(
        self, method: str, path: str, body: Any = None
    ) -> tuple[int, Any]:
        kwargs: dict[str, Any] = {}
        if body is not None:
            kwargs["data"] = body if isinstance(body, str) else json.dumps(body)
        resp = await self.client.request(method, path, **kwargs)
        text = await resp.text()
        return resp.status, json.loads(text) if text else None


async def _start(
    data_dir: Path, options: dict[str, Any], env_vars: dict[str, str]
) -> tuple[Env, Database]:
    await asyncio.to_thread(data_dir.mkdir, parents=True, exist_ok=True)
    db = await Database.open(data_dir / "hagent.db")
    config = Config(
        ha_url="ws://unused.invalid/api/websocket",
        ha_token="unused",  # noqa: S106 - not a secret
        data_dir=data_dir,
        options={"providers": provider_options(**options)},
    )
    ha = HAClient(config.ha_url, config.ha_token)  # never started
    ctx = AppContext(config, db, ha, StateCache(ha), "test-installation")
    hub = ChangeEvents()
    settings = SettingsComponent(hub, env=env_vars)
    fake = FakeProviders()
    providers = ProvidersComponent(
        http_client=fake.client(), settings=settings, price_updater=FakeUpdater
    )
    http = HttpServer()
    http.add_routes(settings.routes())
    await settings.start(ctx)
    await providers.start(ctx)
    client: TestClient[Any, Any] = TestClient(TestServer(http.make_app()))
    await client.start_server()
    env = Env(settings, providers, client, fake)
    hub.subscribe(env.events.append)
    return env, db


async def _stop(env: Env, db: Database) -> None:
    await env.client.close()
    await env.providers.stop()
    await env.settings.stop()
    await db.close()


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    started, db = await _start(tmp_path / "a", {}, {})
    try:
        yield started
    finally:
        await _stop(started, db)


def _no_key(text: str) -> None:
    assert NEW_KEY not in text
    assert OPENAI_KEY not in text


async def test_get_shows_keys_only_masked(env: Env) -> None:
    status, body = await env.request("GET", "/api/settings")
    assert status == 200
    # The option key is in use, but the API shows only a hint.
    assert body["keys"]["openai"] == {
        "set": True,
        "hint": "…" + OPENAI_KEY[-4:],
        "source": "options",
    }
    _no_key(json.dumps(body))
    status, body = await env.request(
        "PUT", "/api/settings/keys/openai", {"key": NEW_KEY}
    )
    assert status == 200
    assert body["keys"]["openai"] == {
        "set": True,
        "hint": "…abcd",
        "source": "settings",
    }
    _no_key(json.dumps(body))
    _no_key(json.dumps([e.to_json() for e in env.events]))
    assert [(e.topic, e.action, e.id) for e in env.events] == [
        ("settings", "updated", "settings")
    ]


async def test_short_key_has_no_hint(env: Env) -> None:
    status, body = await env.request(
        "PUT", "/api/settings/keys/openrouter", {"key": "short-key"}
    )
    assert status == 200
    assert body["keys"]["openrouter"]["hint"] is None
    assert "short-key" not in json.dumps(body)


async def test_invalid_key_errors_hold_no_key(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    bad = NEW_KEY + " with space"
    status, body = await env.request("PUT", "/api/settings/keys/openai", {"key": bad})
    assert (status, body["error"]["code"]) == (422, "invalid_key")
    assert NEW_KEY not in json.dumps(body)
    status, body = await env.request(
        "PUT", "/api/settings/keys/openai", {"key": NEW_KEY, "x": 1}
    )
    assert (status, body["error"]["code"]) == (400, "invalid_body")
    assert NEW_KEY not in json.dumps(body)
    status, _ = await env.request("PUT", "/api/settings/keys/other", {"key": NEW_KEY})
    assert status == 404
    _no_key(caplog.text)


async def test_providers_use_the_settings_key_at_once(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    request = DecisionRequest(DecisionModel("openai", "gpt-6-luna"), VALUES, QUESTIONS)
    env.fake.reply(route("openai", "/decisions"), Reply("decision-openai"))
    await env.providers.decisions.decide(request, agent_id=None)
    assert env.fake.seen[-1].headers["authorization"] == f"Bearer {OPENAI_KEY}"

    await env.request("PUT", "/api/settings/keys/openai", {"key": NEW_KEY})
    env.fake.reply(route("openai", "/decisions"), Reply("decision-openai"))
    await env.providers.decisions.decide(request, agent_id=None)
    assert env.fake.seen[-1].headers["authorization"] == f"Bearer {NEW_KEY}"

    # An error that echoes the key is redacted, and no log holds the key.
    env.fake.reply(
        route("openai", "/decisions"),
        Reply(status=401, text=f'{{"error": "bad key {NEW_KEY}"}}'),
    )
    with pytest.raises(ProviderHTTPError) as info:
        await env.providers.decisions.decide(request, agent_id=None)
    _no_key(str(info.value))
    _no_key(caplog.text)

    # A delete returns to the option key.
    status, body = await env.request("DELETE", "/api/settings/keys/openai")
    assert status == 200
    assert body["keys"]["openai"]["source"] == "options"
    env.fake.reply(route("openai", "/decisions"), Reply("decision-openai"))
    await env.providers.decisions.decide(request, agent_id=None)
    assert env.fake.seen[-1].headers["authorization"] == f"Bearer {OPENAI_KEY}"


async def test_settings_key_wins_over_the_environment(tmp_path: Path) -> None:
    env_key = "sk-env-test-SENTINEL-0004-wxyz"
    env, db = await _start(tmp_path, {}, {"HAGENT_OPENAI_API_KEY": env_key})
    try:
        _, body = await env.request("GET", "/api/settings")
        assert body["keys"]["openai"]["source"] == "environment"
        assert env_key not in json.dumps(body)
        await env.request("PUT", "/api/settings/keys/openai", {"key": NEW_KEY})
        key = env.settings.keys.key("openai")
        assert key is not None
        assert key.reveal() == NEW_KEY
    finally:
        await _stop(env, db)


async def test_settings_survive_a_restart(tmp_path: Path) -> None:
    env, db = await _start(tmp_path, {}, {})
    await env.request("PUT", "/api/settings/keys/openrouter", {"key": NEW_KEY})
    await env.request("PATCH", "/api/settings", {"installation_daily_budget_usd": 7})
    await _stop(env, db)
    env, db = await _start(tmp_path, {}, {})
    try:
        _, body = await env.request("GET", "/api/settings")
        assert body["keys"]["openrouter"]["source"] == "settings"
        assert body["settings"]["installation_daily_budget_usd"] == 7
        assert env.providers.meter.installation_budget_usd() == Decimal(7)
    finally:
        await _stop(env, db)


async def test_budgets_follow_the_settings(env: Env) -> None:
    meter = env.providers.meter
    assert meter.installation_budget_usd() == Decimal("5.0")
    assert await meter.agent_budget_usd("x" * 32) == Decimal("0.5")
    status, body = await env.request(
        "PATCH",
        "/api/settings",
        {"installation_daily_budget_usd": 2.5, "agent_daily_budget_usd": 0.25},
    )
    assert status == 200, body
    assert body["settings"]["installation_daily_budget_usd"] == 2.5
    assert body["defaults"]["installation_daily_budget_usd"] == 5.0
    assert meter.installation_budget_usd() == Decimal("2.5")
    assert await meter.agent_budget_usd("x" * 32) == Decimal("0.25")
    # null returns to the option value.
    await env.request("PATCH", "/api/settings", {"installation_daily_budget_usd": None})
    assert meter.installation_budget_usd() == Decimal("5.0")
    assert await meter.agent_budget_usd("x" * 32) == Decimal("0.25")


async def test_price_overrides_follow_the_settings(env: Env) -> None:
    usage = TokenUsage(input_tokens=1_000_000, output_tokens=0)
    prices = env.providers.meter.prices
    status, _ = await env.request(
        "PATCH",
        "/api/settings",
        {"price_overrides": {"openai:custom-model": {"input": 3, "output": 9}}},
    )
    assert status == 200
    cost = prices.cost("openai", "custom-model", usage)
    assert (cost.usd, cost.source) == (Decimal(3), "override")
    await env.request("PATCH", "/api/settings", {"price_overrides": None})
    with pytest.raises(PriceUnknownError):
        prices.cost("openai", "custom-model", usage)


async def test_decision_models_extend_the_built_in_list(env: Env) -> None:
    _, body = await env.request("GET", "/api/settings")
    assert len(body["built_in_decision_models"]) == len(DEFAULT_DECISION_MODELS)
    status, _ = await env.request(
        "PATCH",
        "/api/settings",
        {"decision_models": [{"provider": "openrouter", "model": "vendor/new-1"}]},
    )
    assert status == 200
    assert env.providers.decision_models[-1] == DecisionModel(
        "openrouter", "vendor/new-1"
    )
    assert len(env.providers.decision_models) == len(DEFAULT_DECISION_MODELS) + 1


async def test_genai_auto_update_toggle(env: Env) -> None:
    FakeUpdater.instances.clear()
    await env.request("PATCH", "/api/settings", {"genai_prices_auto_update": True})
    assert [u.running for u in FakeUpdater.instances] == [True]
    await env.request("PATCH", "/api/settings", {"genai_prices_auto_update": False})
    assert [u.running for u in FakeUpdater.instances] == [False]


@pytest.mark.parametrize(
    "body",
    [
        {"installation_daily_budget_usd": -1},
        {"installation_daily_budget_usd": "5"},
        {"agent_daily_budget_usd": True},
        {"price_overrides": {"no-colon": {"input": 1}}},
        {"price_overrides": {"openai:m": {"output": 1}}},
        {"decision_models": [{"provider": "other", "model": "m"}]},
        {"approvals": {"approvers": ["not-an-id"]}},
        {"approvals": {"approvers": [APPROVER], "timeout_seconds": 0}},
        {
            "approvals": {
                "approvers": [APPROVER],
                "notify_services": {APPROVER: ["notify.persistent"]},
            }
        },
        {"approvals": {"approvers": [], "notify_services": {APPROVER: ["x"]}}},
        {"unknown": 1},
    ],
)
async def test_invalid_settings_are_refused(env: Env, body: dict[str, Any]) -> None:
    status, data = await env.request("PATCH", "/api/settings", body)
    assert (status, data["error"]["code"]) == (422, "settings_invalid"), data
    assert data["error"]["details"]
    _, after = await env.request("GET", "/api/settings")
    assert all(v is None for v in after["settings"].values())


async def test_invalid_json_and_body(env: Env) -> None:
    status, data = await env.request("PATCH", "/api/settings", "{nope")
    assert (status, data["error"]["code"]) == (400, "invalid_json")
    status, data = await env.request("PATCH", "/api/settings", "[]")
    assert (status, data["error"]["code"]) == (400, "invalid_body")


async def test_approval_settings_reader(env: Env) -> None:
    assert env.settings.approvals() is None
    status, body = await env.request(
        "PATCH",
        "/api/settings",
        {
            "approvals": {
                "approvers": [APPROVER],
                "notify_services": {APPROVER: ["notify.mobile_app_test_phone"]},
                "timeout_seconds": 120,
            }
        },
    )
    assert status == 200, body
    approvals = env.settings.approvals()
    assert approvals is not None
    assert approvals.approvers == (APPROVER,)
    assert approvals.timeout_seconds == 120
    # The notify. prefix is dropped, as in the approvals option section.
    assert approvals.notify_services == {APPROVER: ["mobile_app_test_phone"]}


async def test_not_ready_before_start() -> None:
    settings = SettingsComponent()
    http = HttpServer()
    http.add_routes(settings.routes())
    async with TestClient(TestServer(http.make_app())) as client:
        resp = await client.get("/api/settings")
        assert resp.status == 503
        assert (await resp.json())["error"]["code"] == "not_ready"
    await asyncio.sleep(0)


async def test_meter_enforces_the_definition_budget(tmp_path: Path) -> None:
    """budget.daily_usd of the definition wins; None uses the settings default."""
    env, db = await _start(tmp_path, {}, {})
    try:
        service = AgentService(db, ChangeEvents())
        sample = AgentDefinition(
            name="Budget agent",
            instructions="Test.",
            models=Models(chat=ModelRef(provider="openai", model="gpt-4o-mini")),
        )
        agent = await service.create(sample)
        meter = env.providers.meter
        # No budget in the definition: the settings default applies.
        assert agent.definition.budget.daily_usd is None
        assert await meter.agent_budget_usd(agent.id) == Decimal("0.5")
        await env.request("PATCH", "/api/settings", {"agent_daily_budget_usd": 0.3})
        assert await meter.agent_budget_usd(agent.id) == Decimal("0.3")

        # A definition budget wins over the default, from the next call on.
        current = agent.definition
        changed = current.model_copy(
            update={"budget": current.budget.model_copy(update={"daily_usd": 0.01})}
        )
        updated = await service.update(agent.id, changed, agent.version)
        assert await meter.agent_budget_usd(agent.id) == Decimal("0.01")
        state = await meter.budget_state(agent.id)
        assert state.budget_usd == Decimal("0.01")
        # USD 0.15 for one million input tokens is above USD 0.01.
        spec = CallSpec(agent.id, "openai", "chat", "gpt-4o-mini", "openai")
        with pytest.raises(BudgetExceededError) as info:
            async with meter.call(spec, TokenUsage(input_tokens=1_000_000)):
                pass
        assert info.value.scope == "agent"

        # A deleted agent uses the default budget again.
        await service.delete(updated.id, "delete")
        assert await meter.agent_budget_usd(agent.id) == Decimal("0.3")
    finally:
        await _stop(env, db)
