"""
Sensor entities for the Jablotron Local integration.

Creates battery, signal strength, and voltage sensors based on
device info obtained via the startup probe.
"""

from typing import TYPE_CHECKING

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfElectricPotential
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
    Set up sensor entities from a config entry.

    Creates sensors based on available device info data - no assumptions
    about device type.
    """
    coordinator = entry.runtime_data.coordinator
    infos = coordinator.data.device_infos

    if not infos or coordinator.panel_config is None:
        return

    entities: list[SensorEntity] = []
    for device in coordinator.panel_config.devices:
        info = infos.get(device.position)
        if info is None:
            continue

        if info.signal is not None:
            entities.append(JablotronSignalSensor(coordinator, entry, device))

        if info.battery is not None:
            entities.append(JablotronBatterySensor(coordinator, entry, device))

        if info.voltage is not None:
            entities.append(
                JablotronVoltageSensor(coordinator, entry, device, "voltage")
            )

        if info.voltage_current is not None:
            entities.append(
                JablotronVoltageSensor(coordinator, entry, device, "voltage_current")
            )

    async_add_entities(entities)


class JablotronBatterySensor(CoordinatorEntity[JablotronCoordinator], SensorEntity):
    """Battery level sensor for a wireless Jablotron device."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: JablotronCoordinator,
        entry: JablotronConfigEntry,
        device: DeviceEntry,
    ) -> None:
        """Initialize the battery sensor."""
        super().__init__(coordinator)
        self._device_position = device.position
        self._attr_unique_id = f"{entry.unique_id}_device_{device.position}_battery"
        self._attr_name = f"{device.name} Battery"
        self._attr_device_info = _device_info(coordinator, entry)

    @property
    def native_value(self) -> int | None:
        """Return the battery level percentage."""
        state: PanelState = self.coordinator.data
        status = state.device_infos.get(self._device_position)
        if status is None:
            return None

        return status.battery


class JablotronSignalSensor(CoordinatorEntity[JablotronCoordinator], SensorEntity):
    """Signal strength sensor for a Jablotron device."""

    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:signal"

    def __init__(
        self,
        coordinator: JablotronCoordinator,
        entry: JablotronConfigEntry,
        device: DeviceEntry,
    ) -> None:
        """Initialize the signal strength sensor."""
        super().__init__(coordinator)
        self._device_position = device.position
        self._attr_unique_id = f"{entry.unique_id}_device_{device.position}_signal"
        self._attr_name = f"{device.name} Signal"
        self._attr_device_info = _device_info(coordinator, entry)

    @property
    def native_value(self) -> int | None:
        """Return the signal strength percentage."""
        state: PanelState = self.coordinator.data
        status = state.device_infos.get(self._device_position)
        if status is None:
            return None

        return status.signal


class JablotronVoltageSensor(CoordinatorEntity[JablotronCoordinator], SensorEntity):
    """Battery voltage sensor for a Jablotron device."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.VOLTAGE
    _attr_native_unit_of_measurement = UnitOfElectricPotential.VOLT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_suggested_display_precision = 1

    def __init__(
        self,
        coordinator: JablotronCoordinator,
        entry: JablotronConfigEntry,
        device: DeviceEntry,
        field: str,
    ) -> None:
        """Initialize the voltage sensor."""
        super().__init__(coordinator)
        self._device_position = device.position
        self._field = field
        suffix = "Voltage" if field == "voltage" else "Voltage Current"
        self._attr_unique_id = f"{entry.unique_id}_device_{device.position}_{field}"
        self._attr_name = f"{device.name} {suffix}"
        self._attr_device_info = _device_info(coordinator, entry)

    @property
    def native_value(self) -> float | None:
        """Return the voltage value."""
        state: PanelState = self.coordinator.data
        info = state.device_infos.get(self._device_position)
        if info is None:
            return None

        return getattr(info, self._field, None)


def _device_info(
    coordinator: JablotronCoordinator, entry: JablotronConfigEntry
) -> DeviceInfo:
    """Build the HA device info for the panel."""
    sysinfo = coordinator.data.system_info
    return DeviceInfo(
        identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
        name=sysinfo.get("name", "Jablotron Panel"),
        manufacturer="Jablotron",
        model=sysinfo.get("model"),
        sw_version=sysinfo.get("firmware"),
        hw_version=sysinfo.get("hardware"),
    )
