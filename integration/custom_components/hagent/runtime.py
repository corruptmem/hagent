"""The runtime of one hagent config entry.

``HagentRuntime`` owns the link client and the agent list that the app
sends. It keeps HA in step with the app:

- After each connect, the app sends the full agent list. The runtime
  removes the device of each agent that is not in the list, also an agent
  that the user deleted while the link was down.
- An ``agent`` message adds or updates one agent. An ``agent_removed``
  message removes its device, and so all its entities.
- When an agent stops allowing Assist, the runtime removes its
  conversation entity.
- A ``result`` message that no caller received as response data fires the
  ``hagent_result`` event (design section 9.2).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    COMMAND_TIMEOUT,
    DOMAIN,
    EVENT_RESULT,
    TRANSPORT_GRACE,
    signal_agents,
    signal_connection,
)
from .link import LinkClient

_LOGGER = logging.getLogger(__name__)

type Agent = dict[str, Any]


class HagentRuntime:
    """The link and the agent list of one config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry[Any]) -> None:
        self.hass = hass
        self.entry = entry
        self.agents: dict[str, Agent] = {}
        self.client: LinkClient | None = None
        self.installation_id: str | None = entry.unique_id
        # Agent IDs that each platform added entities for.
        self.added: dict[str, set[str]] = {}

    # ------------------------------------------------------------ state

    @property
    def connected(self) -> bool:
        """True while the link to the app is open."""
        return self.client is not None and self.client.connected

    @property
    def hub_identifier(self) -> tuple[str, str]:
        """The device registry identifier of the hagent device."""
        return (DOMAIN, f"app_{self.entry.entry_id}")

    def tracked(self, platform: str) -> set[str]:
        """The agent IDs that ``platform`` added entities for."""
        return self.added.setdefault(platform, set())

    # -------------------------------------------------------- callbacks

    @callback
    def on_connection(self, connected: bool) -> None:
        """The link opened or closed."""
        async_dispatcher_send(
            self.hass, signal_connection(self.entry.entry_id), connected
        )

    @callback
    def on_hello(self, hello: Mapping[str, Any]) -> None:
        """Replace the agent list with the full list from the app."""
        self.installation_id = str(hello.get("installation_id") or "") or None
        agents = {str(a["id"]): dict(a) for a in hello.get("agents") or []}
        for agent_id in set(self.agents) - set(agents):
            self._forget(agent_id)
        for agent_id, agent in agents.items():
            old = self.agents.get(agent_id)
            if old is not None and old.get("assist") and not agent.get("assist"):
                self._remove_conversation(agent_id)
        self.agents = agents
        self._remove_stale_devices()
        async_dispatcher_send(self.hass, signal_agents(self.entry.entry_id))

    @callback
    def on_message(self, msg: Mapping[str, Any]) -> None:
        """Handle an ``agent``, ``agent_removed`` or ``result`` message."""
        kind = msg.get("type")
        if kind == "agent":
            agent = dict(msg["agent"])
            agent_id = str(agent["id"])
            old = self.agents.get(agent_id)
            if old is not None and old.get("assist") and not agent.get("assist"):
                self._remove_conversation(agent_id)
            self.agents[agent_id] = agent
            async_dispatcher_send(self.hass, signal_agents(self.entry.entry_id))
        elif kind == "agent_removed":
            agent_id = str(msg["agent_id"])
            self.agents.pop(agent_id, None)
            self._forget(agent_id)
            async_dispatcher_send(self.hass, signal_agents(self.entry.entry_id))
        elif kind == "result" and not msg.get("delivered"):
            self.hass.bus.async_fire(EVENT_RESULT, dict(msg.get("result") or {}))

    # ---------------------------------------------------------- registry

    def _forget(self, agent_id: str) -> None:
        """Remove the device of ``agent_id`` and all its entities."""
        for ids in self.added.values():
            ids.discard(agent_id)
        devices = dr.async_get(self.hass)
        device = devices.async_get_device_by_identifier(
            (DOMAIN, agent_id), self.entry.entry_id
        )
        if device is not None:
            devices.async_update_device(
                device.id, remove_config_entry_id=self.entry.entry_id
            )

    def _remove_conversation(self, agent_id: str) -> None:
        self.tracked("conversation").discard(agent_id)
        entities = er.async_get(self.hass)
        entity_id = entities.async_get_entity_id(
            "conversation", DOMAIN, f"{agent_id}_conversation"
        )
        if entity_id is not None:
            entities.async_remove(entity_id)

    def _remove_stale_devices(self) -> None:
        """Remove agent devices that HA kept from an earlier run."""
        devices = dr.async_get(self.hass)
        for device in dr.async_entries_for_config_entry(devices, self.entry.entry_id):
            agent_ids = [
                ident for domain, ident in device.identifiers if domain == DOMAIN
            ]
            if not agent_ids or (DOMAIN, agent_ids[0]) == self.hub_identifier:
                continue
            if agent_ids[0] not in self.agents:
                devices.async_update_device(
                    device.id, remove_config_entry_id=self.entry.entry_id
                )

    # ---------------------------------------------------------- commands

    def _client(self) -> LinkClient:
        if self.client is None:
            from .link import LinkUnavailableError  # noqa: PLC0415

            msg = "the hagent integration is not running"

            raise LinkUnavailableError(msg)
        return self.client

    async def invoke(self, fields: Mapping[str, Any]) -> Any:
        """Send an ``invoke`` command and return its response data."""
        timeout = float(fields.get("timeout", 30)) + TRANSPORT_GRACE
        if fields.get("wait") is False:
            timeout = COMMAND_TIMEOUT
        return await self._client().call("invoke", fields, timeout)

    async def set_enabled(self, agent_id: str, enabled: bool) -> None:
        """Turn an agent on or off in the app."""
        await self._client().call(
            "set_enabled", {"agent_id": agent_id, "enabled": enabled}, COMMAND_TIMEOUT
        )

    def agent_for_device(self, device_id: str) -> str | None:
        """The agent ID of an HA device, or ``None``."""
        device = dr.async_get(self.hass).async_get(device_id)
        if device is None or self.entry.entry_id not in device.config_entries:
            return None
        for domain, ident in device.identifiers:
            if domain == DOMAIN and (domain, ident) != self.hub_identifier:
                return ident
        return None
