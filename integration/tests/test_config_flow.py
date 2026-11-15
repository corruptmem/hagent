"""Tests of the hagent config flow: by hand and by Supervisor discovery."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import patch

import pytest
from custom_components.hagent.const import DOMAIN
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from .conftest import FakeApp, make_entry


@pytest.fixture(autouse=True)
def no_entry_setup() -> Iterator[None]:
    """The flow tests check the flow only; the entry does not start a link."""
    with (
        patch("custom_components.hagent.async_setup_entry", return_value=True),
        patch("custom_components.hagent.async_unload_entry", return_value=True),
    ):
        yield


def _user_input(app: FakeApp, **changes: object) -> dict[str, object]:
    return {"host": "127.0.0.1", "port": app.port, "secret": app.secret, **changes}


async def test_user_flow_creates_the_entry(
    hass: HomeAssistant, fake_app: FakeApp
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _user_input(fake_app)
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == _user_input(fake_app)
    assert result["result"].unique_id == "installation-1"


async def test_user_flow_reports_a_bad_secret(
    hass: HomeAssistant, fake_app: FakeApp
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _user_input(fake_app, secret="wrong")
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_flow_reports_an_unreachable_app(
    hass: HomeAssistant, fake_app: FakeApp
) -> None:
    port = fake_app.port
    await fake_app.stop()
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _user_input(fake_app, port=port)
    )
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_aborts_for_a_configured_app(
    hass: HomeAssistant, fake_app: FakeApp
) -> None:
    make_entry(fake_app, port=1).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _user_input(fake_app)
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    # The entry takes the new port.
    assert hass.config_entries.async_entries(DOMAIN)[0].data["port"] == fake_app.port


def _discovery(app: FakeApp, secret: str | None = None) -> HassioServiceInfo:
    return HassioServiceInfo(
        config={
            "host": "127.0.0.1",
            "port": app.port,
            "secret": secret or app.secret,
        },
        name="hagent",
        slug="local_hagent",
        uuid="1234",
    )


async def test_hassio_discovery_asks_and_creates_the_entry(
    hass: HomeAssistant, fake_app: FakeApp
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_HASSIO},
        data=_discovery(fake_app),
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "hassio_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["secret"] == fake_app.secret


async def test_hassio_discovery_with_a_bad_secret_aborts(
    hass: HomeAssistant, fake_app: FakeApp
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_HASSIO},
        data=_discovery(fake_app, secret="wrong"),
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "invalid_auth"


async def test_hassio_discovery_never_changes_a_configured_app(
    hass: HomeAssistant, fake_app: FakeApp
) -> None:
    # Another app sends a discovery with its own host and secret, and its
    # fake link answers with the installation ID of the configured app.
    entry = make_entry(fake_app, host="app.local", secret="old")
    entry.add_to_hass(hass)
    with patch.object(hass.config_entries, "async_reload") as reload:
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_HASSIO},
            data=_discovery(fake_app),
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data["host"] == "app.local"
    assert entry.data["secret"] == "old"
    reload.assert_not_called()
