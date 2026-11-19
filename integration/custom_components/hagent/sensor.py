"""The status, last-result and cost-today sensors of each agent."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import STATE_MAX_CHARS
from .entity import HagentAgentEntity, HagentConfigEntry, setup_agent_platform, short
from .runtime import HagentRuntime

STATUS_OPTIONS = ["idle", "running", "budget_spent", "off", "error"]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HagentConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the sensors of each agent."""

    def factory(runtime: HagentRuntime, agent_id: str) -> list[Entity]:
        return [
            AgentStatusSensor(runtime, agent_id),
            AgentLastResultSensor(runtime, agent_id),
            AgentCostSensor(runtime, agent_id),
        ]

    setup_agent_platform(hass, entry, async_add_entities, "sensor", factory)


class AgentStatusSensor(HagentAgentEntity, SensorEntity):
    """idle, running, budget spent, turned off or error."""

    _attr_translation_key = "status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STATUS_OPTIONS

    def __init__(self, runtime: HagentRuntime, agent_id: str) -> None:
        super().__init__(runtime, agent_id, "status")

    @property
    def native_value(self) -> str | None:
        """The status from the app."""
        status = self.agent.get("status")
        return status if status in STATUS_OPTIONS else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The number of running invocations and the definition version."""
        return {
            "running": self.agent.get("running", 0),
            "version": self.agent.get("version"),
        }


class AgentLastResultSensor(HagentAgentEntity, SensorEntity):
    """The last result. The state is short; the attributes hold the result."""

    _attr_translation_key = "last_result"

    def __init__(self, runtime: HagentRuntime, agent_id: str) -> None:
        super().__init__(runtime, agent_id, "last_result")

    @property
    def native_value(self) -> str | None:
        """The result text, cut to the HA state limit."""
        last = self.agent.get("last_result") or {}
        if not last:
            return None
        if last.get("status") in {"error", "cancelled"}:
            error = last.get("error") or {}
            return short(f"error: {error.get('code', last.get('status'))}", 255)
        return short(last.get("result"), STATE_MAX_CHARS)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The full last result object from the app."""
        return dict(self.agent.get("last_result") or {})


class AgentCostSensor(HagentAgentEntity, SensorEntity):
    """The cost of the agent today, in USD."""

    _attr_translation_key = "cost_today"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "USD"
    _attr_suggested_display_precision = 4

    def __init__(self, runtime: HagentRuntime, agent_id: str) -> None:
        super().__init__(runtime, agent_id, "cost_today")

    @property
    def native_value(self) -> float | None:
        """The spend of today."""
        value = self.agent.get("cost_today_usd")
        return None if value is None else float(value)
