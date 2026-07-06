"""
Jablotron Local integration.

Local control of Jablotron JA-100+ alarm panels via USB HID,
bypassing the Jablonet cloud entirely.

Entry setup flow:

1. Open the USB HID connection and start the background reader thread.
2. Wait for initial sysinfo and section state data from the panel.
3. If a service PIN is configured:
   a. Export panel config from FLEXI_CFG (device/section names).
   b. Probe all devices for status (battery, signal, voltage).
4. Forward platform setup (alarm_control_panel, binary_sensor, sensor).

The coordinator runs a periodic refresh (configurable, default 30 min)
to re-probe device status, keeping battery and signal readings current.
"""

from typing import TYPE_CHECKING

from homeassistant.const import Platform
from homeassistant.exceptions import ConfigEntryNotReady

from .client import (
    JablotronAuthError,
    JablotronClient,
    JablotronCommandError,
    JablotronConnectionError,
)
from .config_flow import (
    CONF_DEVICE_PATH,
    CONF_PROBE_INTERVAL,
    CONF_SERVICE_PIN,
    DEFAULT_PROBE_INTERVAL,
)
from .config_reader import (
    ConfigReadError,
    DeviceEntry,
    PanelConfig,
    find_flexi_cfg_device,
    read_panel_config,
)
from .const import LOGGER
from .coordinator import JablotronCoordinator
from .data import JablotronConfigEntry, JablotronData
from .protocol import CODE_PREFIX_WILDCARD

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

PLATFORMS: list[Platform] = [
    Platform.ALARM_CONTROL_PANEL,
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
]


async def async_setup_entry(hass: HomeAssistant, entry: JablotronConfigEntry) -> bool:
    """
    Set up Jablotron Local from a config entry.

    Opens the USB HID connection, starts the background reader thread,
    waits for initial panel data, then optionally exports and reads the
    panel config from FLEXI_CFG (requires service PIN).
    """
    device_path = entry.data[CONF_DEVICE_PATH]
    service_pin = entry.data.get(CONF_SERVICE_PIN)
    probe_interval = entry.data.get(CONF_PROBE_INTERVAL, DEFAULT_PROBE_INTERVAL)

    client = JablotronClient(path=device_path)
    try:
        await hass.async_add_executor_job(client.connect)
    except JablotronConnectionError as err:
        msg = f"Failed to open panel at {device_path}"
        raise ConfigEntryNotReady(msg) from err

    coordinator = JablotronCoordinator(
        hass,
        client,
        service_pin=service_pin,
        probe_interval=probe_interval,
    )
    await coordinator.async_config_entry_first_refresh()

    # Wait briefly for initial sysinfo and section data to arrive from the
    # panel. The reader thread has already sent queries at this point; the
    # panel typically responds within ~50ms. We give it up to 2s.
    await coordinator.async_wait_for_initial_data()

    # Read panel config and probe device status if service PIN is configured.
    if service_pin:
        panel_config = await hass.async_add_executor_job(
            _export_and_read_config, client, device_path, service_pin
        )
        coordinator.panel_config = panel_config

        # Probe device status (battery, signal) in a separate session.
        devices = panel_config.devices if panel_config else []
        if devices:
            statuses = await hass.async_add_executor_job(
                _probe_device_status, client, service_pin, devices
            )
            coordinator.data.device_infos = {s.device_number: s for s in statuses}

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


def _export_and_read_config(
    client: JablotronClient, hidraw_path: str, service_pin: str
) -> PanelConfig | None:
    """
    Trigger config export and read from FLEXI_CFG.

    Sequence:
    1. Find FLEXI_CFG block device (sibling of hidraw).
    2. Authenticate with service PIN and trigger export.
    3. Read exported config from the block device.

    Non-fatal: returns ``None`` on any failure.

    Args:
        client: Connected HID client.
        hidraw_path: The hidraw device path.
        service_pin: Service/installer PIN (digits only, no prefix).

    """
    # 1. Find block device.
    block_device = find_flexi_cfg_device(hidraw_path)
    if block_device is None:
        LOGGER.info(
            "FLEXI_CFG block device not found for %s; "
            "device and section names will use defaults",
            hidraw_path,
        )
        return None

    # 2. Trigger config export via authenticated HID session.
    try:
        code = CODE_PREFIX_WILDCARD + service_pin
        client.export_config(code)
    except JablotronAuthError:
        LOGGER.warning(
            "Service PIN rejected by panel; device and section names will use defaults"
        )
        return None
    except JablotronCommandError as err:
        LOGGER.warning(
            "Config export failed: %s; device and section names will use defaults",
            err.detail,
        )
        return None

    # 3. Read from block device while session is still open.
    try:
        return read_panel_config(block_device)
    except ConfigReadError as err:
        LOGGER.warning(
            "Failed to read panel config from %s: %s; "
            "device and section names will use defaults",
            block_device,
            err.detail,
        )
        return None
    finally:
        # 4. Always end the authenticated session after reading.
        client.end_session()


def _probe_device_status(
    client: JablotronClient, service_pin: str, devices: list[DeviceEntry]
) -> list:
    """
    Probe devices for status (battery, signal) in a single session.

    Non-fatal: returns empty list on any failure.

    Args:
        client: Connected HID client.
        service_pin: Service/installer PIN (digits only).
        devices: List of devices to probe.

    """
    try:
        code = CODE_PREFIX_WILDCARD + service_pin
        return client.probe_all_devices(code, devices)
    except JablotronAuthError:
        LOGGER.warning("Service PIN rejected during device probe")
        return []
    except JablotronCommandError as err:
        LOGGER.warning("Device probe failed: %s", err.detail)
        return []
