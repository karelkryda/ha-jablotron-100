"""Tests for custom_components.jablotron_local.alarm_control_panel."""

from unittest.mock import MagicMock, patch

import pytest
from homeassistant.components.alarm_control_panel import AlarmControlPanelState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.jablotron_local.alarm_control_panel import (
    _SECONDARY_STATE_MAP,
    _STATE_MAP,
    JablotronAlarmPanel,
    _map_alarm_state,
    async_setup_entry,
)
from custom_components.jablotron_local.client import (
    JablotronAuthError,
    JablotronCommandError,
)
from custom_components.jablotron_local.coordinator import JablotronCoordinator
from custom_components.jablotron_local.protocol import (
    ArmMode,
    SectionPrimaryState,
    SectionSecondaryState,
    SectionState,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_section(
    number: int = 1,
    primary: SectionPrimaryState = SectionPrimaryState.DISARMED,
    secondary: SectionSecondaryState = SectionSecondaryState.NORMAL,
) -> SectionState:
    """Create a SectionState for testing."""
    return SectionState(number=number, primary=primary, secondary=secondary, flags=0)


def _make_coordinator(
    hass: HomeAssistant,
    sections: list[SectionState] | None = None,
) -> JablotronCoordinator:
    """Create a coordinator with mocked client and given sections."""
    client = MagicMock()
    client.on_packets = None
    client.on_connection_change = None
    client.command_in_progress = False

    coordinator = JablotronCoordinator(hass, client)
    if sections:
        coordinator.data.sections = sections
    coordinator.data.system_info = {
        "model": "JA-103K",
        "name": "Test Panel",
    }
    return coordinator


def _make_entry(unique_id: str = "test_serial_123") -> MagicMock:
    """Create a mock config entry."""
    entry = MagicMock()
    entry.unique_id = unique_id
    entry.entry_id = "mock_entry_id"
    return entry


def _make_entity(
    hass: HomeAssistant,
    section: SectionState | None = None,
    coordinator: JablotronCoordinator | None = None,
) -> JablotronAlarmPanel:
    """Create a JablotronAlarmPanel entity for testing."""
    if section is None:
        section = _make_section()
    if coordinator is None:
        coordinator = _make_coordinator(hass, sections=[section])

    entry = _make_entry()
    entity = JablotronAlarmPanel(coordinator, entry, section)
    entity.hass = hass
    return entity


# ---------------------------------------------------------------------------
# Entity setup
# ---------------------------------------------------------------------------


class TestEntitySetup:
    async def test_creates_entity_per_section(self, hass: HomeAssistant):
        sections = [
            _make_section(1, SectionPrimaryState.DISARMED),
            _make_section(2, SectionPrimaryState.ARMED_FULL),
            _make_section(3, SectionPrimaryState.ARMED_PARTIAL),
        ]
        coordinator = _make_coordinator(hass, sections=sections)
        entry = _make_entry()
        entry.runtime_data = MagicMock()
        entry.runtime_data.coordinator = coordinator

        entities: list = []
        await async_setup_entry(hass, entry, entities.extend)

        assert len(entities) == 3

    async def test_entity_unique_id(self, hass: HomeAssistant):
        entity = _make_entity(hass, section=_make_section(2))

        assert entity.unique_id == "test_serial_123_section_2"

    async def test_entity_name(self, hass: HomeAssistant):
        entity = _make_entity(hass, section=_make_section(3))

        assert entity.name == "Section 3"


# ---------------------------------------------------------------------------
# State mapping
# ---------------------------------------------------------------------------


class TestStateMapping:
    @pytest.mark.parametrize(
        ("primary", "expected"),
        [
            (SectionPrimaryState.DISARMED, AlarmControlPanelState.DISARMED),
            (
                SectionPrimaryState.ARMED_FULL,
                AlarmControlPanelState.ARMED_AWAY,
            ),
            (
                SectionPrimaryState.ARMED_PARTIAL,
                AlarmControlPanelState.ARMED_HOME,
            ),
            (
                SectionPrimaryState.MAINTENANCE,
                AlarmControlPanelState.DISARMED,
            ),
            (SectionPrimaryState.SERVICE, AlarmControlPanelState.DISARMED),
            (SectionPrimaryState.BLOCKED, AlarmControlPanelState.DISARMED),
        ],
    )
    async def test_state_map_covers_all_values(
        self,
        hass: HomeAssistant,
        primary: SectionPrimaryState,
        expected: AlarmControlPanelState,
    ):
        section = _make_section(1, primary)
        entity = _make_entity(hass, section=section)

        assert entity.alarm_state == expected

    async def test_state_updates_on_coordinator_change(self, hass: HomeAssistant):
        section = _make_section(1, SectionPrimaryState.DISARMED)
        coordinator = _make_coordinator(hass, sections=[section])
        entity = _make_entity(hass, section=section, coordinator=coordinator)

        # Simulate coordinator update: section is now armed
        coordinator.data.sections = [
            _make_section(1, SectionPrimaryState.ARMED_FULL),
        ]

        with patch.object(entity, "async_write_ha_state"):
            entity._handle_coordinator_update()

        assert entity.alarm_state == AlarmControlPanelState.ARMED_AWAY

    async def test_state_map_dict_complete(self):
        """Verify _STATE_MAP covers all known active primary states."""
        for state in SectionPrimaryState:
            if state not in (
                SectionPrimaryState.UNSET,
                SectionPrimaryState.OFF,
                SectionPrimaryState.UNKNOWN,
            ):
                assert state in _STATE_MAP

    async def test_secondary_state_map_complete(self):
        """Verify _SECONDARY_STATE_MAP covers all known non-NORMAL secondary states."""
        for state in SectionSecondaryState:
            if state not in (
                SectionSecondaryState.NORMAL,
                SectionSecondaryState.UNKNOWN,
            ):
                assert state in _SECONDARY_STATE_MAP

    async def test_arming_state_overrides_primary(self, hass: HomeAssistant):
        """Secondary ARMING maps to HA ARMING regardless of primary."""
        section = _make_section(
            1, SectionPrimaryState.ARMED_FULL, SectionSecondaryState.ARMING
        )
        entity = _make_entity(hass, section=section)

        assert entity.alarm_state == AlarmControlPanelState.ARMING

    async def test_arming_partial_maps_to_arming(self, hass: HomeAssistant):
        """Arming towards ARMED_PARTIAL also maps to HA ARMING."""
        section = _make_section(
            1, SectionPrimaryState.ARMED_PARTIAL, SectionSecondaryState.ARMING
        )
        entity = _make_entity(hass, section=section)

        assert entity.alarm_state == AlarmControlPanelState.ARMING

    async def test_arming_to_armed_transition(self, hass: HomeAssistant):
        """Entity transitions from ARMING to ARMED_AWAY when exit delay expires."""
        section = _make_section(
            1, SectionPrimaryState.ARMED_FULL, SectionSecondaryState.ARMING
        )
        coordinator = _make_coordinator(hass, sections=[section])
        entity = _make_entity(hass, section=section, coordinator=coordinator)

        assert entity.alarm_state == AlarmControlPanelState.ARMING

        # Exit delay expires - panel sends ARMED_FULL without ARMING
        coordinator.data.sections = [
            _make_section(
                1, SectionPrimaryState.ARMED_FULL, SectionSecondaryState.NORMAL
            ),
        ]

        with patch.object(entity, "async_write_ha_state"):
            entity._handle_coordinator_update()

        assert entity.alarm_state == AlarmControlPanelState.ARMED_AWAY

    async def test_pending_state_maps_to_pending(self, hass: HomeAssistant):
        """Secondary PENDING maps to HA PENDING (entry delay)."""
        section = _make_section(
            1, SectionPrimaryState.ARMED_FULL, SectionSecondaryState.PENDING
        )
        entity = _make_entity(hass, section=section)

        assert entity.alarm_state == AlarmControlPanelState.PENDING

    async def test_pending_to_disarmed_transition(self, hass: HomeAssistant):
        """Entity transitions from PENDING to DISARMED when user disarms."""
        section = _make_section(
            1, SectionPrimaryState.ARMED_FULL, SectionSecondaryState.PENDING
        )
        coordinator = _make_coordinator(hass, sections=[section])
        entity = _make_entity(hass, section=section, coordinator=coordinator)

        assert entity.alarm_state == AlarmControlPanelState.PENDING

        coordinator.data.sections = [
            _make_section(
                1, SectionPrimaryState.DISARMED, SectionSecondaryState.NORMAL
            ),
        ]

        with patch.object(entity, "async_write_ha_state"):
            entity._handle_coordinator_update()

        assert entity.alarm_state == AlarmControlPanelState.DISARMED

    async def test_map_alarm_state_normal_secondary_uses_primary(self):
        """When secondary is NORMAL, primary state map is used."""
        section = SectionState(
            number=1,
            primary=SectionPrimaryState.ARMED_FULL,
            secondary=SectionSecondaryState.NORMAL,
            flags=0,
        )
        assert _map_alarm_state(section) == AlarmControlPanelState.ARMED_AWAY

    async def test_map_alarm_state_secondary_overrides_primary(self):
        """When secondary is not NORMAL, secondary map takes priority."""
        section = SectionState(
            number=1,
            primary=SectionPrimaryState.DISARMED,
            secondary=SectionSecondaryState.ARMING,
            flags=0,
        )
        # Even though primary is DISARMED, ARMING secondary wins
        assert _map_alarm_state(section) == AlarmControlPanelState.ARMING

    async def test_entity_keeps_last_state_when_section_disappears(
        self, hass: HomeAssistant
    ):
        """If section is missing from coordinator data, entity keeps last state."""
        section = _make_section(1, SectionPrimaryState.ARMED_FULL)
        coordinator = _make_coordinator(hass, sections=[section])
        entity = _make_entity(hass, section=section, coordinator=coordinator)

        assert entity.alarm_state == AlarmControlPanelState.ARMED_AWAY

        # Section disappears from data (e.g. unknown byte skipped it)
        coordinator.data.sections = []

        with patch.object(entity, "async_write_ha_state"):
            entity._handle_coordinator_update()

        # Should keep ARMED_AWAY, not reset to None
        assert entity.alarm_state == AlarmControlPanelState.ARMED_AWAY

    async def test_full_arm_lifecycle(self, hass: HomeAssistant):
        """Test full lifecycle: disarmed → arming → armed → pending → disarmed."""
        section = _make_section(1, SectionPrimaryState.DISARMED)
        coordinator = _make_coordinator(hass, sections=[section])
        entity = _make_entity(hass, section=section, coordinator=coordinator)

        assert entity.alarm_state == AlarmControlPanelState.DISARMED

        # User arms → exit delay
        coordinator.data.sections = [
            _make_section(
                1, SectionPrimaryState.ARMED_FULL, SectionSecondaryState.ARMING
            ),
        ]
        with patch.object(entity, "async_write_ha_state"):
            entity._handle_coordinator_update()
        assert entity.alarm_state == AlarmControlPanelState.ARMING

        # Exit delay expires → fully armed
        coordinator.data.sections = [
            _make_section(
                1, SectionPrimaryState.ARMED_FULL, SectionSecondaryState.NORMAL
            ),
        ]
        with patch.object(entity, "async_write_ha_state"):
            entity._handle_coordinator_update()
        assert entity.alarm_state == AlarmControlPanelState.ARMED_AWAY

        # Intrusion detected → entry delay
        coordinator.data.sections = [
            _make_section(
                1, SectionPrimaryState.ARMED_FULL, SectionSecondaryState.PENDING
            ),
        ]
        with patch.object(entity, "async_write_ha_state"):
            entity._handle_coordinator_update()
        assert entity.alarm_state == AlarmControlPanelState.PENDING

        # User disarms in time
        coordinator.data.sections = [
            _make_section(
                1, SectionPrimaryState.DISARMED, SectionSecondaryState.NORMAL
            ),
        ]
        with patch.object(entity, "async_write_ha_state"):
            entity._handle_coordinator_update()
        assert entity.alarm_state == AlarmControlPanelState.DISARMED


# ---------------------------------------------------------------------------
# Arm/disarm commands
# ---------------------------------------------------------------------------


class TestArmDisarmCommands:
    async def test_arm_away_calls_modify_section(self, hass: HomeAssistant):
        section = _make_section(2, SectionPrimaryState.DISARMED)
        coordinator = _make_coordinator(hass, sections=[section])
        entity = _make_entity(hass, section=section, coordinator=coordinator)

        await entity.async_alarm_arm_away("1234")

        coordinator.client.modify_section.assert_called_once_with(
            2, ArmMode.ARM_AWAY, "9991234"
        )

    async def test_arm_home_calls_modify_section(self, hass: HomeAssistant):
        section = _make_section(1, SectionPrimaryState.DISARMED)
        coordinator = _make_coordinator(hass, sections=[section])
        entity = _make_entity(hass, section=section, coordinator=coordinator)

        await entity.async_alarm_arm_home("5678")

        coordinator.client.modify_section.assert_called_once_with(
            1, ArmMode.ARM_HOME, "9995678"
        )

    async def test_disarm_calls_modify_section(self, hass: HomeAssistant):
        section = _make_section(3, SectionPrimaryState.ARMED_FULL)
        coordinator = _make_coordinator(hass, sections=[section])
        entity = _make_entity(hass, section=section, coordinator=coordinator)

        await entity.async_alarm_disarm("4321")

        coordinator.client.modify_section.assert_called_once_with(
            3, ArmMode.DISARM, "9994321"
        )

    async def test_wrong_code_raises_ha_error(self, hass: HomeAssistant):
        section = _make_section(1, SectionPrimaryState.DISARMED)
        coordinator = _make_coordinator(hass, sections=[section])
        entity = _make_entity(hass, section=section, coordinator=coordinator)

        coordinator.client.modify_section.side_effect = JablotronAuthError()

        with pytest.raises(HomeAssistantError) as exc_info:
            await entity.async_alarm_arm_away("0000")

        assert exc_info.value.translation_key == "wrong_code"

    async def test_command_error_raises_ha_error(self, hass: HomeAssistant):
        section = _make_section(1, SectionPrimaryState.DISARMED)
        coordinator = _make_coordinator(hass, sections=[section])
        entity = _make_entity(hass, section=section, coordinator=coordinator)

        coordinator.client.modify_section.side_effect = JablotronCommandError("timeout")

        with pytest.raises(HomeAssistantError) as exc_info:
            await entity.async_alarm_arm_away("1234")

        assert exc_info.value.translation_key == "command_failed"

    async def test_missing_code_raises_service_validation_error(
        self, hass: HomeAssistant
    ):
        entity = _make_entity(hass)

        with pytest.raises(ServiceValidationError) as exc_info:
            await entity.async_alarm_arm_away(None)

        assert exc_info.value.translation_key == "code_required"

    async def test_empty_code_raises_service_validation_error(
        self, hass: HomeAssistant
    ):
        entity = _make_entity(hass)

        with pytest.raises(ServiceValidationError) as exc_info:
            await entity.async_alarm_disarm("")

        assert exc_info.value.translation_key == "code_required"

    async def test_code_prefix_prepended(self, hass: HomeAssistant):
        """The entity prepends '999' to the user's PIN."""
        section = _make_section(1, SectionPrimaryState.DISARMED)
        coordinator = _make_coordinator(hass, sections=[section])
        entity = _make_entity(hass, section=section, coordinator=coordinator)

        await entity.async_alarm_arm_away("9876")

        # Full code should be "999" + "9876"
        call_args = coordinator.client.modify_section.call_args
        assert call_args[0][2] == "9999876"
