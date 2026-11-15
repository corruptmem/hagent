"""The config flow of the hagent integration.

Two ways lead to an entry:

- Supervisor discovery: the hagent app sends its host, port and secret to
  the Supervisor. HA then offers the set-up, and the user confirms it.
  A discovery never changes an entry that exists.
- By hand: the user enters the host, the port and the secret. The app
  shows them in ``GET api/integration`` (see docs/api.md).

Each way connects once and reads the ``hello`` message. The installation ID
from that message is the unique ID of the entry.
"""

from __future__ import annotations

from typing import Any

import probatio
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from .const import CONF_SECRET, DEFAULT_PORT, DOMAIN
from .link import (
    LinkAuthError,
    LinkError,
    LinkProtocolError,
    link_url,
    open_link,
)

USER_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_HOST): str,
        probatio.Required(CONF_PORT, default=DEFAULT_PORT): probatio.All(
            probatio.Coerce(int), probatio.Range(min=1, max=65535)
        ),
        probatio.Required(CONF_SECRET): str,
    }
)


class HagentConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up the link to the hagent app."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovery: dict[str, Any] | None = None

    async def _check(self, data: dict[str, Any]) -> tuple[str | None, str | None]:
        """Connect once. Return (installation ID, error key)."""
        try:
            ws, hello = await open_link(
                async_get_clientsession(self.hass),
                link_url(data[CONF_HOST], int(data[CONF_PORT])),
                data[CONF_SECRET],
                heartbeat=None,
            )
        except LinkAuthError:
            return None, "invalid_auth"
        except LinkProtocolError:
            return None, "protocol"
        except LinkError:
            return None, "cannot_connect"
        await ws.close()
        return str(hello.get("installation_id") or ""), None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the host, the port and the secret."""
        errors: dict[str, str] = {}
        if user_input is not None:
            installation_id, error = await self._check(user_input)
            if error is None:
                await self.async_set_unique_id(installation_id)
                self._abort_if_unique_id_configured(updates=user_input)
                return self.async_create_entry(title="hagent", data=user_input)
            errors["base"] = error
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_hassio(
        self, discovery_info: HassioServiceInfo
    ) -> ConfigFlowResult:
        """Receive the discovery from the hagent app."""
        config = discovery_info.config
        self._discovery = {
            CONF_HOST: str(config[CONF_HOST]),
            CONF_PORT: int(config[CONF_PORT]),
            CONF_SECRET: str(config[CONF_SECRET]),
        }
        installation_id, error = await self._check(self._discovery)
        if error is not None:
            return self.async_abort(reason=error)
        await self.async_set_unique_id(installation_id)
        # Never change an existing entry from a discovery: another app could
        # send a host and secret of its own. Only a manual set-up with the
        # same installation ID changes an entry.
        self._abort_if_unique_id_configured()
        return await self.async_step_hassio_confirm()

    async def async_step_hassio_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask the user to confirm the discovered app."""
        assert self._discovery is not None  # noqa: S101 - set by the hassio step
        if user_input is not None:
            return self.async_create_entry(title="hagent", data=self._discovery)
        return self.async_show_form(
            step_id="hassio_confirm",
            description_placeholders={"host": self._discovery[CONF_HOST]},
        )
