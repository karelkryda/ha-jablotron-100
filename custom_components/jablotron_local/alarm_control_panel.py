"""
Alarm control panel entities for the Jablotron Local integration.

Creates one :class:`AlarmControlPanelEntity` per active section
reported by the panel. State is pushed by the panel via the
coordinator - no polling. Arm/disarm commands authenticate per-action
with the user's PIN via the client's blocking command path.
"""

from typing import TYPE_CHECKING

from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity,
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
    CodeFormat,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .client import JablotronAuthError, JablotronCommandError
from .const import DOMAIN
from .coordinator import JablotronCoordinator, PanelState
from .protocol import (
    CODE_PREFIX_WILDCARD,
    ArmMode,
    SectionPrimaryState,
    SectionSecondaryState,
    SectionState,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .data import JablotronConfigEntry

# Map panel primary state to HA alarm state.
_STATE_MAP: dict[SectionPrimaryState, AlarmControlPanelState] = {
    SectionPrimaryState.DISARMED: AlarmControlPanelState.DISARMED,
    SectionPrimaryState.ARMED_PARTIAL: AlarmControlPanelState.ARMED_HOME,
    SectionPrimaryState.ARMED_FULL: AlarmControlPanelState.ARMED_AWAY,
    SectionPrimaryState.MAINTENANCE: AlarmControlPanelState.DISARMED,
    SectionPrimaryState.SERVICE: AlarmControlPanelState.DISARMED,
    SectionPrimaryState.BLOCKED: AlarmControlPanelState.DISARMED,
}

# Map panel secondary (transitional) state to HA alarm state.
# When a secondary state is present (not NORMAL), it overrides the primary mapping.
_SECONDARY_STATE_MAP: dict[SectionSecondaryState, AlarmControlPanelState] = {
    SectionSecondaryState.ARMING: AlarmControlPanelState.ARMING,
}


def _map_alarm_state(section: SectionState) -> AlarmControlPanelState | None:
    """
    Map a panel section state to an HA alarm control panel state.

    Secondary (transitional) states take priority over the primary
    state when present. Falls back to :data:`_STATE_MAP` when the
    secondary state is NORMAL.

    Args:
        section: The decoded section state from the panel.

    Returns:
        The corresponding HA alarm state, or ``None`` if unmapped.

    """
    if section.secondary != SectionSecondaryState.NORMAL:
        return _SECONDARY_STATE_MAP.get(section.secondary)

    return _STATE_MAP.get(section.primary)


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: JablotronConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """
    Set up alarm control panel entities from a config entry.

    Creates one entity per active section reported by the panel on
    initial connection. Sections with primary state OFF are excluded.
    """
    coordinator = entry.runtime_data.coordinator
    sections = coordinator.data.sections

    entities = [
        JablotronAlarmPanel(coordinator, entry, section) for section in sections
    ]
    async_add_entities(entities)


class JablotronAlarmPanel(
    CoordinatorEntity[JablotronCoordinator], AlarmControlPanelEntity
):
    """
    Alarm control panel entity for a single Jablotron section.

    State is updated via the coordinator's push mechanism.
    Arm/disarm commands authenticate per-action through the client.
    """

    _attr_has_entity_name = True
    _attr_supported_features = (
        AlarmControlPanelEntityFeature.ARM_AWAY
        | AlarmControlPanelEntityFeature.ARM_HOME
    )
    _attr_code_arm_required = True
    _attr_code_format = CodeFormat.NUMBER

    def __init__(
        self,
        coordinator: JablotronCoordinator,
        entry: JablotronConfigEntry,
        section: SectionState,
    ) -> None:
        """
        Initialize the alarm panel entity.

        Args:
            coordinator: The push-style coordinator instance.
            entry: The config entry this entity belongs to.
            section: The initial section state from the panel.

        """
        super().__init__(coordinator)
        self._section_number = section.number
        self._attr_unique_id = f"{entry.unique_id}_section_{section.number}"

        # Use the parsed section name from FLEXI_CFG config if available.
        section_name = coordinator.get_section_name(section.number)
        self._attr_name = section_name or f"Section {section.number}"

        self._attr_device_info = _device_info(coordinator, entry)
        self._attr_alarm_state = _map_alarm_state(section)

    def _handle_coordinator_update(self) -> None:
        """Update _attr_alarm_state from coordinator data, then write state."""
        section = self._find_section()
        if section is not None:
            self._attr_alarm_state = _map_alarm_state(section)

        self.async_write_ha_state()

    def _find_section(self) -> SectionState | None:
        """Find this entity's section in the coordinator data."""
        state: PanelState = self.coordinator.data
        for section in state.sections:
            if section.number == self._section_number:
                return section

        return None

    async def async_alarm_disarm(self, code: str | None = None) -> None:
        """Send disarm command to the panel."""
        await self._async_command(ArmMode.DISARM, code)

    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        """Send arm-away command to the panel."""
        await self._async_command(ArmMode.ARM_AWAY, code)

    async def async_alarm_arm_home(self, code: str | None = None) -> None:
        """Send arm-home command to the panel."""
        await self._async_command(ArmMode.ARM_HOME, code)

    async def _async_command(self, mode: ArmMode, code: str | None) -> None:
        """
        Execute an arm/disarm command via the client.

        The user enters only their PIN (e.g. "1234"). The integration
        prepends the default prefix ("999") to form the full code sent
        to the panel.

        Args:
            mode: The desired arm mode.
            code: PIN entered by the user (digits only).

        Raises:
            ServiceValidationError: If no code was provided.
            HomeAssistantError: On wrong code or command failure.

        """
        if not code:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="code_required",
            )

        try:
            full_code = CODE_PREFIX_WILDCARD + code
            await self.hass.async_add_executor_job(
                self.coordinator.client.modify_section,
                self._section_number,
                mode,
                full_code,
            )
        except JablotronAuthError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="wrong_code",
            ) from err
        except JablotronCommandError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
            ) from err


def _device_info(
    coordinator: JablotronCoordinator, entry: JablotronConfigEntry
) -> DeviceInfo:
    """
    Build the HA device info for the panel.

    All section entities share the same device - the physical panel.
    System info fields (model, firmware, etc.) are populated from the
    coordinator's sysinfo cache.
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
