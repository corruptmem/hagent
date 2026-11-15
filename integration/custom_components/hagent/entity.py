"""Base classes of the hagent entities."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN, MANUFACTURER, signal_agents, signal_connection
from .runtime import Agent, HagentRuntime

type HagentConfigEntry = ConfigEntry[HagentRuntime]


class HagentAgentEntity(Entity):
    """An entity of one agent device."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, runtime: HagentRuntime, agent_id: str, key: str) -> None:
        self.runtime = runtime
        self.agent_id = agent_id
        self._attr_unique_id = f"{agent_id}_{key}"
        agent = runtime.agents.get(agent_id, {})
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, agent_id)},
            name=str(agent.get("name") or agent_id),
            manufacturer=MANUFACTURER,
            model="Agent",
            sw_version=f"definition {agent.get('version')}",
        )

    @property
    def agent(self) -> Agent:
        """The agent snapshot from the app. Empty if the agent is gone."""
        return self.runtime.agents.get(self.agent_id, {})

    @property
    def available(self) -> bool:
        """Available while the link is open and the app knows the agent."""
        return self.runtime.connected and self.agent_id in self.runtime.agents

    async def async_added_to_hass(self) -> None:
        """Write the state again on each agent or link change."""
        entry_id = self.runtime.entry.entry_id
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_agents(entry_id), self._async_changed
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_connection(entry_id), self._async_connection
            )
        )

    @callback
    def _async_changed(self) -> None:
        self.async_write_ha_state()

    @callback
    def _async_connection(self, _connected: bool) -> None:
        self.async_write_ha_state()


def setup_agent_platform(  # noqa: PLR0913, PLR0917
    hass: HomeAssistant,
    entry: HagentConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    platform: str,
    factory: Callable[[HagentRuntime, str], list[Entity]],
    wanted: Callable[[Agent], bool] = lambda _agent: True,
) -> None:
    """Add the entities of ``platform`` for each new agent, now and later."""
    runtime = entry.runtime_data

    @callback
    def add_new() -> None:
        tracked = runtime.tracked(platform)
        new: list[Entity] = []
        for agent_id, agent in runtime.agents.items():
            if agent_id not in tracked and wanted(agent):
                tracked.add(agent_id)
                new.extend(factory(runtime, agent_id))
        if new:
            async_add_entities(new)

    entry.async_on_unload(
        async_dispatcher_connect(hass, signal_agents(entry.entry_id), add_new)
    )
    add_new()


def short(value: Any, limit: int) -> str | None:
    """A text form of ``value`` that fits in an HA state."""
    if value is None:
        return None
    text = value if isinstance(value, str) else repr(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"
