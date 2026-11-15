"""The enabled switch of each agent."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import HagentAgentEntity, HagentConfigEntry, setup_agent_platform
from .link import LinkError
from .runtime import HagentRuntime


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HagentConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the enabled switch of each agent."""

    def factory(runtime: HagentRuntime, agent_id: str) -> list[Entity]:
        return [AgentEnabledSwitch(runtime, agent_id)]

    setup_agent_platform(hass, entry, async_add_entities, "switch", factory)


class AgentEnabledSwitch(HagentAgentEntity, SwitchEntity):
    """Turns the agent on or off in the app."""

    _attr_translation_key = "enabled"

    def __init__(self, runtime: HagentRuntime, agent_id: str) -> None:
        super().__init__(runtime, agent_id, "enabled")

    @property
    def is_on(self) -> bool | None:
        """True if the agent is on."""
        value = self.agent.get("enabled")
        return None if value is None else bool(value)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the agent on."""
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the agent off."""
        await self._set(False)

    async def _set(self, enabled: bool) -> None:
        try:
            await self.runtime.set_enabled(self.agent_id, enabled)
        except LinkError as err:
            msg = f"hagent: {err}"
            raise HomeAssistantError(msg) from err
