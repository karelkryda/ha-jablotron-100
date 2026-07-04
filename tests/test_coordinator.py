"""Tests for custom_components.jablotron_local.coordinator."""

import logging
from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant

from custom_components.jablotron_local.coordinator import JablotronCoordinator
from custom_components.jablotron_local.protocol import (
    Packet,
    PacketType,
    SectionPrimaryState,
    SectionState,
    SysInfoType,
    UiControl,
    UiStatusReason,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_coordinator(hass: HomeAssistant) -> JablotronCoordinator:
    """Create a coordinator with a mocked client."""
    client = MagicMock()
    client.on_packets = None
    client.on_connection_change = None
    client.command_in_progress = False
    return JablotronCoordinator(hass, client)


def _sections_packet(
    section_states: list[tuple[int, int]],
) -> Packet:
    """
    Build a SECTIONS packet from (primary, flags) pairs.

    Unused slots are filled with SectionPrimaryState.OFF (7).
    """
    data = bytearray()
    for primary, flags in section_states:
        data.append(primary)
        data.append(flags)
    # Pad to 16 slots + 2-byte trailer (matching real panel format)
    while len(data) < 34:
        data.append(SectionPrimaryState.OFF)
        data.append(0x00)
    return Packet(PacketType.SECTIONS, bytes(data))


def _devices_states_packet(active_devices: set[int]) -> Packet:
    """Build a DEVICES_STATES packet with the given active device numbers."""
    max_device = max(active_devices) if active_devices else 0
    num_bytes = max(1, (max_device + 7) // 8)
    bitmap = 0
    for device in active_devices:
        bitmap |= 1 << device
    bitmap_bytes = bitmap.to_bytes(num_bytes, byteorder="little")
    data = b"\x00" + bitmap_bytes
    return Packet(PacketType.DEVICES_STATES, data)


def _sysinfo_packet(kind: SysInfoType, value: str) -> Packet:
    """Build a SYS_INFO packet."""
    data = bytes([kind]) + value.encode("ascii") + b"\x00"
    return Packet(PacketType.SYS_INFO, data)


def _wrong_code_packet() -> Packet:
    """Build a UI_CONTROL STATUS WRONG_CODE packet."""
    return Packet(
        PacketType.UI_CONTROL,
        bytes([UiControl.STATUS, UiStatusReason.WRONG_CODE]),
    )


# ---------------------------------------------------------------------------
# Sections update
# ---------------------------------------------------------------------------


class TestSectionsUpdate:
    async def test_sections_parsed_and_stored(self, hass: HomeAssistant):
        coordinator = _make_coordinator(hass)
        packet = _sections_packet(
            [
                (SectionPrimaryState.DISARMED, 0x00),
                (SectionPrimaryState.ARMED_FULL, 0x00),
                (SectionPrimaryState.ARMED_PARTIAL, 0x00),
            ]
        )

        coordinator._process_packets([packet])

        assert len(coordinator.data.sections) == 3
        assert coordinator.data.sections[0] == SectionState(
            number=1, primary=SectionPrimaryState.DISARMED, flags=0
        )
        assert coordinator.data.sections[1] == SectionState(
            number=2, primary=SectionPrimaryState.ARMED_FULL, flags=0
        )
        assert coordinator.data.sections[2] == SectionState(
            number=3, primary=SectionPrimaryState.ARMED_PARTIAL, flags=0
        )

    async def test_sections_update_notifies_entities(self, hass: HomeAssistant):
        coordinator = _make_coordinator(hass)
        callback = MagicMock()
        coordinator.async_add_listener(callback)

        packet = _sections_packet([(SectionPrimaryState.DISARMED, 0x00)])
        coordinator._process_packets([packet])

        callback.assert_called()

    async def test_no_notification_when_sections_unchanged(self, hass: HomeAssistant):
        coordinator = _make_coordinator(hass)
        packet = _sections_packet([(SectionPrimaryState.DISARMED, 0x00)])

        # First update
        coordinator._process_packets([packet])

        callback = MagicMock()
        coordinator.async_add_listener(callback)

        # Same sections again - no notification
        coordinator._process_packets([packet])
        callback.assert_not_called()

    async def test_section_state_change_triggers_notification(
        self, hass: HomeAssistant
    ):
        coordinator = _make_coordinator(hass)
        packet1 = _sections_packet([(SectionPrimaryState.DISARMED, 0x00)])
        coordinator._process_packets([packet1])

        callback = MagicMock()
        coordinator.async_add_listener(callback)

        packet2 = _sections_packet([(SectionPrimaryState.ARMED_FULL, 0x00)])
        coordinator._process_packets([packet2])

        callback.assert_called()


# ---------------------------------------------------------------------------
# Device activity update
# ---------------------------------------------------------------------------


class TestDeviceActivityUpdate:
    async def test_active_devices_parsed(self, hass: HomeAssistant):
        coordinator = _make_coordinator(hass)
        packet = _devices_states_packet({1, 3, 5})

        coordinator._process_packets([packet])

        assert coordinator.data.active_devices == frozenset({1, 3, 5})

    async def test_activity_change_notifies(self, hass: HomeAssistant):
        coordinator = _make_coordinator(hass)
        callback = MagicMock()
        coordinator.async_add_listener(callback)

        packet = _devices_states_packet({2, 4})
        coordinator._process_packets([packet])

        callback.assert_called()

    async def test_no_notification_when_activity_unchanged(self, hass: HomeAssistant):
        coordinator = _make_coordinator(hass)
        packet = _devices_states_packet({1, 2})
        coordinator._process_packets([packet])

        callback = MagicMock()
        coordinator.async_add_listener(callback)

        # Same activity again
        coordinator._process_packets([packet])
        callback.assert_not_called()


# ---------------------------------------------------------------------------
# System info parsing
# ---------------------------------------------------------------------------


class TestSysinfoParsing:
    async def test_model_stored(self, hass: HomeAssistant):
        coordinator = _make_coordinator(hass)
        packet = _sysinfo_packet(SysInfoType.MODEL, "JA-103K")

        coordinator._process_packets([packet])

        assert coordinator.data.system_info["model"] == "JA-103K"

    async def test_firmware_stored(self, hass: HomeAssistant):
        coordinator = _make_coordinator(hass)
        packet = _sysinfo_packet(SysInfoType.FIRMWARE, "MD6112.07.0")

        coordinator._process_packets([packet])

        assert coordinator.data.system_info["firmware"] == "MD6112.07.0"

    async def test_hardware_stored(self, hass: HomeAssistant):
        coordinator = _make_coordinator(hass)
        packet = _sysinfo_packet(SysInfoType.HARDWARE, "MD15005")

        coordinator._process_packets([packet])

        assert coordinator.data.system_info["hardware"] == "MD15005"

    async def test_name_stored(self, hass: HomeAssistant):
        coordinator = _make_coordinator(hass)
        packet = _sysinfo_packet(SysInfoType.NAME, "My Panel")

        coordinator._process_packets([packet])

        assert coordinator.data.system_info["name"] == "My Panel"

    async def test_initial_data_ready_after_sections_and_model(
        self, hass: HomeAssistant
    ):
        coordinator = _make_coordinator(hass)

        # Model alone doesn't signal ready
        coordinator._process_packets(
            [
                _sysinfo_packet(SysInfoType.MODEL, "JA-103K"),
            ]
        )
        assert not coordinator._initial_data_ready.is_set()

        # Sections arrive -> now ready
        packet = _sections_packet([(SectionPrimaryState.DISARMED, 0x00)])
        coordinator._process_packets([packet])
        assert coordinator._initial_data_ready.is_set()


# ---------------------------------------------------------------------------
# Wrong code handling
# ---------------------------------------------------------------------------


class TestWrongCodeHandling:
    async def test_wrong_code_during_command_does_not_log_critical(
        self, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
    ):
        coordinator = _make_coordinator(hass)
        coordinator.client.command_in_progress = True

        with caplog.at_level(logging.CRITICAL):
            coordinator._process_packets([_wrong_code_packet()])

        assert "WRONG_CODE" not in caplog.text

    async def test_wrong_code_without_command_logs_critical(
        self, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
    ):
        coordinator = _make_coordinator(hass)
        coordinator.client.command_in_progress = False

        with caplog.at_level(logging.CRITICAL):
            coordinator._process_packets([_wrong_code_packet()])

        assert "WRONG_CODE" in caplog.text

    async def test_non_wrong_code_status_does_not_log_critical(
        self, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
    ):
        coordinator = _make_coordinator(hass)
        coordinator.client.command_in_progress = False

        # NO_SESSION status (0x06) should not trigger critical log
        packet = Packet(
            PacketType.UI_CONTROL,
            bytes([UiControl.STATUS, UiStatusReason.NO_SESSION]),
        )

        with caplog.at_level(logging.CRITICAL):
            coordinator._process_packets([packet])

        assert "WRONG_CODE" not in caplog.text


# ---------------------------------------------------------------------------
# Connection state
# ---------------------------------------------------------------------------


class TestConnectionState:
    async def test_connection_change_updates_state(self, hass: HomeAssistant):
        coordinator = _make_coordinator(hass)

        assert not coordinator.data.connected

        coordinator._process_connection_change(connected=True)
        assert coordinator.data.connected

        coordinator._process_connection_change(connected=False)
        assert not coordinator.data.connected

    async def test_connection_change_notifies_entities(self, hass: HomeAssistant):
        coordinator = _make_coordinator(hass)
        callback = MagicMock()
        coordinator.async_add_listener(callback)

        coordinator._process_connection_change(connected=True)
        callback.assert_called()

    async def test_duplicate_connection_state_no_notification(
        self, hass: HomeAssistant
    ):
        coordinator = _make_coordinator(hass)
        coordinator._process_connection_change(connected=True)

        callback = MagicMock()
        coordinator.async_add_listener(callback)

        # Same state again - no notification
        coordinator._process_connection_change(connected=True)
        callback.assert_not_called()
