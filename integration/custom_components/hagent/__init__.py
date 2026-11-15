"""The hagent integration: the HA side of the hagent agent manager.

The integration is thin. It holds no agent configuration. It keeps one
link to the hagent app (``link.py``), shows one device per agent, and
forwards invocations: the ``hagent.invoke`` action, the AI Task entities
and the Assist conversation entities. See ``docs/link.md`` and design
sections 6, 9.2, 9.3 and 15.
"""

from __future__ import annotations

from typing import Any

import probatio
from homeassistant.const import ATTR_DEVICE_ID, ATTR_ENTITY_ID, CONF_HOST, CONF_PORT
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .const import (
    ATTR_DATA,
    ATTR_ON_TIMEOUT,
    ATTR_TIMEOUT,
    ATTR_WAIT,
    CONF_SECRET,
    DEFAULT_TIMEOUT,
    DOMAIN,
    MAX_TIMEOUT,
    SERVICE_INVOKE,
)
from .entity import HagentConfigEntry
from .invoke import link_error, result_data
from .link import LinkClient, LinkError, link_url
from .runtime import HagentRuntime

PLATFORMS = ["binary_sensor", "sensor", "switch", "ai_task", "conversation"]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

INVOKE_SCHEMA = probatio.Schema(
    {
        probatio.Optional(ATTR_DEVICE_ID): probatio.All(cv.ensure_list, [cv.string]),
        probatio.Optional(ATTR_ENTITY_ID): cv.entity_ids,
        probatio.Optional(ATTR_DATA): dict,
        probatio.Optional(ATTR_WAIT, default=True): cv.boolean,
        probatio.Optional(ATTR_TIMEOUT, default=DEFAULT_TIMEOUT): probatio.All(
            probatio.Coerce(float), probatio.Range(min=0.1, max=MAX_TIMEOUT)
        ),
        probatio.Optional(ATTR_ON_TIMEOUT): probatio.In(["cancel", "continue"]),
    }
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the ``hagent.invoke`` action."""

    async def handle_invoke(call: ServiceCall) -> ServiceResponse:
        return await _async_invoke(hass, call)

    hass.services.async_register(
        DOMAIN,
        SERVICE_INVOKE,
        handle_invoke,
        schema=INVOKE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: HagentConfigEntry) -> bool:
    """Start the link. The entry loads also when the app is down."""
    runtime = HagentRuntime(hass, entry)
    runtime.client = LinkClient(
        async_get_clientsession(hass),
        link_url(entry.data[CONF_HOST], int(entry.data[CONF_PORT])),
        entry.data[CONF_SECRET],
        on_hello=runtime.on_hello,
        on_message=runtime.on_message,
        on_connection=runtime.on_connection,
    )
    entry.runtime_data = runtime
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_create_background_task(
        hass, runtime.client.run(), name=f"hagent link {entry.entry_id}"
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HagentConfigEntry) -> bool:
    """Stop the link and unload the platforms."""
    runtime = entry.runtime_data
    if runtime.client is not None:
        await runtime.client.stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


def _target_agents(
    hass: HomeAssistant, call: ServiceCall
) -> list[tuple[HagentRuntime, str]]:
    """The agents that the action targets: devices, or entities of devices."""
    runtimes = [
        entry.runtime_data for entry in hass.config_entries.async_loaded_entries(DOMAIN)
    ]
    device_ids = list(call.data.get(ATTR_DEVICE_ID, []))
    entities = er.async_get(hass)
    for entity_id in call.data.get(ATTR_ENTITY_ID, []):
        entry = entities.async_get(entity_id)
        if entry is None or entry.platform != DOMAIN or entry.device_id is None:
            msg = f"{entity_id} is not an entity of a hagent agent"
            raise ServiceValidationError(msg)
        device_ids.append(entry.device_id)
    found: list[tuple[HagentRuntime, str]] = []
    for device_id in dict.fromkeys(device_ids):
        for runtime in runtimes:
            agent_id = runtime.agent_for_device(device_id)
            if agent_id is not None:
                if (runtime, agent_id) not in found:
                    found.append((runtime, agent_id))
                break
        else:
            msg = f"device {device_id} is not a hagent agent"
            raise ServiceValidationError(msg)
    return found


async def _async_invoke(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    targets = _target_agents(hass, call)
    if len(targets) != 1:
        msg = "hagent.invoke needs exactly one agent device or agent entity"
        raise ServiceValidationError(msg)
    runtime, agent_id = targets[0]
    fields: dict[str, Any] = {
        "agent_id": agent_id,
        "source": "invoke",
        "data": dict(call.data.get(ATTR_DATA) or {}),
        "wait": call.data[ATTR_WAIT],
        "timeout": call.data[ATTR_TIMEOUT],
        "caller": {
            "context_id": call.context.id,
            "user_id": call.context.user_id,
        },
    }
    if ATTR_ON_TIMEOUT in call.data:
        fields["on_timeout"] = call.data[ATTR_ON_TIMEOUT]
    try:
        data = await runtime.invoke(fields)
    except LinkError as err:
        raise link_error(err) from err
    if not isinstance(data, dict):
        msg = "hagent: the app sent no response data"
        raise HomeAssistantError(msg)
    result_data(data)  # raises for status error or cancelled
    return data if call.return_response else None
