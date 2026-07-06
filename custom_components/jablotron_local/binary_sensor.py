"""
Binary sensor entities for the Jablotron Local integration.

Creates one :class:`BinarySensorEntity` per device parsed from the
panel configuration (FLEXI_CFG). State is driven by the 0xd8
DEVICES_STATES bitmap pushed by the panel - bit N active means the
device at position N is currently triggered (motion detected, door
open, flood detected, etc.).

All devices from the panel config are exposed. Users can disable
entities they don't need via the HA UI.
"""

from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
)
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import JablotronCoordinator, PanelState

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .config_reader import DeviceEntry
    from .data import JablotronConfigEntry


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: JablotronConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """
    Set up binary sensor entities from a config entry.

    Creates one entity per device from the panel config. If panel
    config is not available (FLEXI_CFG not readable), no entities are
    created.
    """
    coordinator = entry.runtime_data.coordinator

    if coordinator.panel_config is None:
        return

    entities = [
        JablotronBinarySensor(coordinator, entry, device)
        for device in coordinator.panel_config.devices
    ]
    async_add_entities(entities)


class JablotronBinarySensor(
    CoordinatorEntity[JablotronCoordinator], BinarySensorEntity
):
    """
    Binary sensor entity for a single Jablotron device.

    State is determined by the device's bit in the 0xd8 DEVICES_STATES
    bitmap: active (bit set) = on, inactive (bit clear) = off.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: JablotronCoordinator,
        entry: JablotronConfigEntry,
        device: DeviceEntry,
    ) -> None:
        """
        Initialize the binary sensor entity.

        Args:
            coordinator: The push-style coordinator instance.
            entry: The config entry this entity belongs to.
            device: The device entry from the panel config.

        """
        super().__init__(coordinator)
        self._device_position = device.position
        self._attr_unique_id = f"{entry.unique_id}_device_{device.position}"
        self._attr_name = device.name
        self._attr_device_info = _device_info(coordinator, entry)

    @property
    def is_on(self) -> bool:
        """Return True if the device is currently active (triggered)."""
        state: PanelState = self.coordinator.data
        return self._device_position in state.active_devices

    def _handle_coordinator_update(self) -> None:
        """Write state on coordinator update."""
        self.async_write_ha_state()


def _device_info(
    coordinator: JablotronCoordinator, entry: JablotronConfigEntry
) -> DeviceInfo:
    """
    Build the HA device info for the panel.

    All device entities share the same parent device - the physical panel.
    """
    sysinfo = coordinator.data.system_info
    return DeviceInfo(
        identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
        name=sysinfo.get("name", "Jablotron Panel"),
        manufacturer="Jablotron",
        model=sysinfo.get("model"),
        sw_version=sysinfo.get("firmware"),
        hw_version=sysinfo.get("hardware"),
    )
