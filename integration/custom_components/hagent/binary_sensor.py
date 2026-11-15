"""The connectivity sensor of the hagent device (design section 6)."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import MANUFACTURER, signal_connection
from .entity import HagentConfigEntry
from .runtime import HagentRuntime


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HagentConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the connectivity sensor."""
    async_add_entities([HagentConnectivity(entry.runtime_data)])


class HagentConnectivity(BinarySensorEntity):
    """On while the link to the hagent app is open."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "link"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, runtime: HagentRuntime) -> None:
        self.runtime = runtime
        self._attr_unique_id = f"{runtime.entry.entry_id}_link"
        self._attr_device_info = DeviceInfo(
            identifiers={runtime.hub_identifier},
            name="hagent",
            manufacturer=MANUFACTURER,
            model="Agent manager",
        )

    @property
    def is_on(self) -> bool:
        """True while the link is open."""
        return self.runtime.connected

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        """The last connection error."""
        client = self.runtime.client
        return {"last_error": None if client is None else client.last_error}

    async def async_added_to_hass(self) -> None:
        """Follow the link state."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_connection(self.runtime.entry.entry_id),
                self._async_connection,
            )
        )

    @callback
    def _async_connection(self, _connected: bool) -> None:
        self.async_write_ha_state()
