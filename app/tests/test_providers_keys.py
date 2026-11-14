"""API keys never reach HA, logs, error texts or call records."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from hagent_app.app import App
from hagent_app.config import Config
from hagent_app.ha import HAClient
from hagent_app.providers import (
    DecisionModel,
    DecisionRequest,
    ProviderHTTPError,
    ProvidersComponent,
    ProvidersConfig,
    Secret,
    YesNoQuestion,
)
from hagent_app.providers.config import ConfigKeySource
from helpers.fake_ha import FakeHA
from helpers.fake_providers import (
    OPENAI_KEY,
    OPENROUTER_KEY,
    QUESTIONS,
    VALUES,
    FakeProviders,
    Reply,
    openrouter_alpha,
    provider_options,
    route,
)
from helpers.providers_env import ProvidersEnv
from helpers.wait import wait_until
from pydantic_ai import Agent

StartEnv = Callable[..., Awaitable[ProvidersEnv]]
KEYS = (OPENAI_KEY, OPENROUTER_KEY)
QUESTION = [YesNoQuestion("q", "Is it cold?")]


def _assert_no_key(text: str) -> None:
    for key in KEYS:
        assert key not in text


def _routes(fake: FakeProviders) -> None:
    fake.reply(route("openai", "/decisions"), Reply("decision-openai"))
    fake.reply(openrouter_alpha("/decisions"), Reply("decision-openrouter"))
    fake.reply(route("openai", "/embeddings"), Reply("embeddings-openai"))
    fake.reply(route("openrouter", "/embeddings"), Reply("embeddings-openrouter"))
    fake.reply(route("openai", "/responses"), Reply("chat-openai-stream"))
    fake.reply(route("openrouter", "/chat/completions"), Reply("chat-openrouter"))


async def _use_every_call_type(component: ProvidersComponent) -> None:
    for model in (
        DecisionModel("openai", "gpt-6-luna"),
        DecisionModel("openrouter", "typesafe/jev-1.14"),
    ):
        await component.decisions.decide(
            DecisionRequest(model, VALUES, QUESTIONS), agent_id="a1"
        )
    await component.embeddings.embed(
        "openai", "text-embedding-3-small", ["a", "b"], agent_id="a1"
    )
    await component.embeddings.embed(
        "openrouter", "openai/text-embedding-3-small", ["a", "b"], agent_id=None
    )
    async with Agent(
        component.chat_model("openai", "gpt-4o-mini", agent_id="a1")
    ).run_stream("hi") as result:
        await result.get_output()
    await Agent(
        component.chat_model("openrouter", "openai/gpt-4o-mini", agent_id="a1")
    ).run("hi")


def test_reprs_hide_keys() -> None:
    config = ProvidersConfig(
        openai_api_key=OPENAI_KEY, openrouter_api_key=OPENROUTER_KEY
    )
    keys = ConfigKeySource(config, env={})
    secret = keys.key("openai")
    assert secret is not None
    assert secret.reveal() == OPENAI_KEY
    for text in (repr(config), str(config), repr(keys), repr(secret), str(secret)):
        _assert_no_key(text)
    assert repr(Secret("x")) == "Secret('***')"


def test_environment_overrides_option_keys() -> None:
    config = ProvidersConfig(openai_api_key="from-options")
    keys = ConfigKeySource(config, env={"HAGENT_OPENAI_API_KEY": "from-env"})
    assert keys.key("openai") == Secret("from-env")
    assert keys.key("openrouter") is None


async def test_keys_absent_from_debug_logs_and_records(
    providers_env: StartEnv, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    env = await providers_env()
    _routes(env.fake)
    await _use_every_call_type(env.component)

    # The keys went to the providers, and only there.
    sent = {s.headers.get("authorization") for s in env.fake.seen}
    assert sent == {f"Bearer {OPENAI_KEY}", f"Bearer {OPENROUTER_KEY}"}
    for seen in env.fake.seen:
        host = seen.url.split("/")[2]
        expected = OPENAI_KEY if host == "api.openai.test" else OPENROUTER_KEY
        assert seen.headers["authorization"] == f"Bearer {expected}"
        _assert_no_key(json.dumps(seen.body))

    assert caplog.records  # the DEBUG capture is active
    _assert_no_key(caplog.text)
    _assert_no_key(json.dumps(await env.calls()))


async def test_error_that_echoes_the_key_is_redacted(
    providers_env: StartEnv, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    env = await providers_env()
    env.fake.reply(
        route("openai", "/decisions"),
        Reply(status=401, text=f'{{"error": "bad key {OPENAI_KEY}"}}'),
    )
    request = DecisionRequest(DecisionModel("openai", "gpt-6-luna"), "x", QUESTION)
    with pytest.raises(ProviderHTTPError) as info:
        await env.component.decisions.decide(request, agent_id="a1")
    assert "bad key ***" in str(info.value)
    _assert_no_key(str(info.value))
    _assert_no_key(caplog.text)
    [row] = await env.calls()
    assert "***" in row["error"]
    _assert_no_key(row["error"])


async def test_keys_never_reach_ha(fake_ha: FakeHA, tmp_path: Path) -> None:
    fake = FakeProviders()
    _routes(fake)
    config = Config(
        ha_url=fake_ha.url,
        ha_token=fake_ha.token,
        data_dir=tmp_path,
        options={"providers": provider_options()},
    )
    component = ProvidersComponent(http_client=fake.client())
    client = HAClient(config.ha_url, config.ha_token, backoff_initial=0.01)
    app = App(config, [component], client=client)
    ctx = await app.start()
    try:
        await ctx.ha.wait_connected()
        await wait_until(lambda: bool(fake_ha.commands_of("get_states")), limit=5)
        await _use_every_call_type(component)
    finally:
        await app.stop()
    assert fake_ha.commands  # HA saw the app's own traffic
    _assert_no_key(json.dumps(fake_ha.commands))
