"""
Jablotron Local integration.

Local control of Jablotron JA-100+ alarm panels via USB HID,
bypassing the Jablonet cloud entirely.
"""

from typing import TYPE_CHECKING

from homeassistant.const import Platform
from homeassistant.exceptions import ConfigEntryNotReady

from .client import JablotronClient, JablotronConnectionError
from .config_flow import CONF_DEVICE_PATH
from .coordinator import JablotronCoordinator
from .data import JablotronConfigEntry, JablotronData

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

PLATFORMS: list[Platform] = [
    Platform.ALARM_CONTROL_PANEL,
]


async def async_setup_entry(hass: HomeAssistant, entry: JablotronConfigEntry) -> bool:
    """
    Set up Jablotron Local from a config entry.

    Opens the USB HID connection, starts the background reader thread,
    creates the push-style coordinator, and forwards platform setup.
    """
    device_path = entry.data[CONF_DEVICE_PATH]

    client = JablotronClient(path=device_path)
    try:
        await hass.async_add_executor_job(client.connect)
    except JablotronConnectionError as err:
        msg = f"Failed to open panel at {device_path}"
        raise ConfigEntryNotReady(msg) from err

    coordinator = JablotronCoordinator(hass, client)
    await coordinator.async_config_entry_first_refresh()

    # Wait briefly for initial sysinfo and section data to arrive from the
    # panel. The reader thread has already sent queries at this point; the
    # panel typically responds within ~50ms. We give it up to 2s.
    await coordinator.async_wait_for_initial_data()

    entry.runtime_data = JablotronData(
        client=client,
        coordinator=coordinator,
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: JablotronConfigEntry) -> bool:
    """Unload a Jablotron Local config entry and disconnect from the panel."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await hass.async_add_executor_job(entry.runtime_data.client.disconnect)

    return unloaded
