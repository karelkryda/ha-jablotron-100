"""
Tests for device status and diagnostic protocol decoders.

Test vectors are synthetic data matching the response formats
documented in DEVICE_STATUS_PROTOCOL.md.
"""

import pytest

from custom_components.jablotron_local.protocol import (
    _decode_battery,
    _decode_signal,
    cmd_diagnostics_force_info,
    cmd_diagnostics_start,
    cmd_diagnostics_stop,
    cmd_query_device_status,
    decode_device_diagnostic,
    decode_device_status,
)

# ---------------------------------------------------------------------------
# cmd_query_device_status
# ---------------------------------------------------------------------------


class TestCmdQueryDeviceStatus:
    def test_builds_correct_packet(self):
        pkt = cmd_query_device_status(15)
        assert pkt.type == 0x52
        assert pkt.data == bytes([0x28, 15])

    def test_device_zero(self):
        pkt = cmd_query_device_status(0)
        assert pkt.data == bytes([0x28, 0])


# ---------------------------------------------------------------------------
# cmd_diagnostics_start / force_info / stop
# ---------------------------------------------------------------------------


class TestCmdDiagnostics:
    def test_start(self):
        pkt = cmd_diagnostics_start(7)
        assert pkt.type == 0x94
        assert pkt.data == bytes([7, 0x01])

    def test_force_info(self):
        pkt = cmd_diagnostics_force_info(7)
        assert pkt.type == 0x96
        assert pkt.data == bytes([7, 0x09, 0x00])

    def test_stop(self):
        pkt = cmd_diagnostics_stop(7)
        assert pkt.type == 0x94
        assert pkt.data == bytes([7, 0x00])


# ---------------------------------------------------------------------------
# decode_device_status - wireless (fc marker)
# ---------------------------------------------------------------------------


class TestDecodeDeviceStatusWireless:
    def test_standard_wireless_pir(self):
        # Device 15, wireless PIR: signal=0x92, battery=0x0a
        data = bytes.fromhex("a80f0004209d00fc920a00")
        status = decode_device_status(data)
        assert status is not None
        assert status.device_number == 15
        assert status.signal == 90  # (0x92 & 0x1f) * 5 = 18 * 5
        assert status.battery == 100  # (0x0a & 0x0f) * 10
        assert status.active is True  # activity byte 0x20 != 0

    def test_wireless_smoke_detector(self):
        # Device 16, smoke: flags=0xc7, signal=0x8e, battery=0x09
        data = bytes.fromhex("a810c70400fd00fc8e0900")
        status = decode_device_status(data)
        assert status is not None
        assert status.device_number == 16
        assert status.signal == 70  # (0x8e & 0x1f) * 5 = 14 * 5
        assert status.battery == 90  # 9 * 10
        assert status.active is False  # activity byte at offset 4 is 0x00

    def test_wireless_never_communicated(self):
        # Key fob that never communicated: activity=ffff, signal=0, battery=0x0b
        data = bytes.fromhex("a814000600fffffc000b00")
        status = decode_device_status(data)
        assert status is not None
        assert status.device_number == 20
        assert status.signal == 0
        assert status.battery is None  # 0x0b >= threshold
        assert status.active is False  # 0xff = never communicated

    def test_wireless_second_channel(self):
        # Second channel of multi-channel device: activity=000000
        data = bytes.fromhex("a8120006000000fc000b00")
        status = decode_device_status(data)
        assert status is not None
        assert status.device_number == 18
        assert status.active is False

    def test_wireless_battery_special_values(self):
        # battery_raw=0x0e (no measurement)
        data = bytes.fromhex("a817000600fffffc670e00")
        status = decode_device_status(data)
        assert status is not None
        assert status.battery is None


# ---------------------------------------------------------------------------
# decode_device_status - wired (f2 marker)
# ---------------------------------------------------------------------------


class TestDecodeDeviceStatusWired:
    def test_wired_pir_active(self):
        # Device 2, wired PIR, active (activity_hi=0x10)
        data = bytes.fromhex("a802ff04100000f200")
        status = decode_device_status(data)
        assert status is not None
        assert status.device_number == 2
        assert status.signal is None
        assert status.battery is None
        assert status.active is True

    def test_wired_pir_idle(self):
        # Device 3, wired PIR, idle
        data = bytes.fromhex("a803ff04000000f200")
        status = decode_device_status(data)
        assert status is not None
        assert status.device_number == 3
        assert status.active is False

    def test_wired_siren_with_state_byte(self):
        # Device 8 with state byte 0x02 after f2
        data = bytes.fromhex("a808ff04000000f202")
        status = decode_device_status(data)
        assert status is not None
        assert status.device_number == 8
        assert status.active is False

    def test_central_unit(self):
        # Device 0, central unit (different flags/byte2 pattern)
        data = bytes.fromhex("a8000003000000f264")
        status = decode_device_status(data)
        assert status is not None
        assert status.device_number == 0
        assert status.signal is None
        assert status.battery is None


# ---------------------------------------------------------------------------
# decode_device_status - unknown format
# ---------------------------------------------------------------------------


class TestDecodeDeviceStatusUnknown:
    def test_radio_module_f3_marker(self):
        # Device 1 with f3 marker (radio module)
        data = bytes.fromhex("a801ff04000000f300")
        status = decode_device_status(data)
        assert status is not None
        assert status.device_number == 1
        assert status.signal is None
        assert status.battery is None
        assert status.active is False

    def test_communicator_truncated(self):
        # Communicator: fc marker but truncated/zeroed
        data = bytes.fromhex("a81d0000000000fc00")
        status = decode_device_status(data)
        assert status is not None
        assert status.device_number == 29

    def test_too_short(self):
        data = bytes([0xA8, 0x00])
        assert decode_device_status(data) is None

    def test_wrong_subtype(self):
        data = bytes([0x28, 0x00, 0xFF, 0x04, 0x00, 0x00, 0x00, 0xF2, 0x00])
        assert decode_device_status(data) is None

    def test_empty_data(self):
        assert decode_device_status(b"") is None


# ---------------------------------------------------------------------------
# decode_device_diagnostic
# ---------------------------------------------------------------------------


class TestDecodeDeviceDiagnostic:
    def test_regular_detector(self):
        # Device 2: flags=0x0f, signal=0x88, rest=ae...
        data = bytes.fromhex("020a080f88ae0000030100")
        diag = decode_device_diagnostic(data)
        assert diag is not None
        assert diag.device_number == 2
        assert diag.signal == 40  # (0x88 & 0x1f) * 5 = 8 * 5
        assert diag.battery is None  # flags 0x0f >= threshold
        assert diag.voltage is None
        assert diag.voltage_current is None

    def test_smoke_detector_with_battery(self):
        # Device 14: flags=0x05, signal=0x88
        data = bytes.fromhex("0e0a0705888318002821")
        diag = decode_device_diagnostic(data)
        assert diag is not None
        assert diag.device_number == 14
        assert diag.signal == 40
        assert diag.battery == 50  # (0x05 & 0x0f) * 10
        assert diag.voltage is None

    def test_siren_with_voltages(self):
        # Device 6: flags=0x0a, voltages 5.88V and 5.67V
        data = bytes.fromhex("060a0a0a886c004c026c013702")
        diag = decode_device_diagnostic(data)
        assert diag is not None
        assert diag.device_number == 6
        assert diag.signal == 40
        assert diag.battery is None  # siren uses voltage not %
        assert diag.voltage == pytest.approx(5.88)  # 0x024c = 588
        assert diag.voltage_current == pytest.approx(5.67)  # 0x0237 = 567

    def test_siren_indoor_equal_voltages(self):
        # Device 7: flags=0x0a, both 3.30V
        data = bytes.fromhex("070a0a0a886c004a016c014a01")
        diag = decode_device_diagnostic(data)
        assert diag is not None
        assert diag.voltage == pytest.approx(3.30)
        assert diag.voltage_current == pytest.approx(3.30)

    def test_keypad_minimal_payload(self):
        # Device 9: flags=0x0f, payload_len=2, just signal
        data = bytes.fromhex("090a020f89")
        diag = decode_device_diagnostic(data)
        assert diag is not None
        assert diag.device_number == 9
        assert diag.signal == 45  # (0x89 & 0x1f) * 5 = 9 * 5
        assert diag.battery is None

    def test_wrong_subtype(self):
        # Second byte is 0x0b instead of 0x0a - not a requested info response.
        data = bytes.fromhex("020b080f88ae0000030100")
        assert decode_device_diagnostic(data) is None

    def test_too_short(self):
        data = bytes.fromhex("020a08")
        assert decode_device_diagnostic(data) is None

    def test_empty_data(self):
        assert decode_device_diagnostic(b"") is None


# ---------------------------------------------------------------------------
# _decode_signal
# ---------------------------------------------------------------------------


class TestDecodeSignal:
    def test_standard_values(self):
        assert _decode_signal(0x84) == 20  # 4 * 5
        assert _decode_signal(0x88) == 40  # 8 * 5
        assert _decode_signal(0x8B) == 55  # 11 * 5
        assert _decode_signal(0x92) == 90  # 18 * 5

    def test_zero(self):
        assert _decode_signal(0x00) == 0

    def test_max_clamp(self):
        # 0x1f * 5 = 155 -> clamped to 100
        assert _decode_signal(0xFF) == 100
        assert _decode_signal(0x1F) == 100  # 31 * 5 = 155 -> 100

    def test_masks_upper_bits(self):
        # 0x72 & 0x1f = 0x12 = 18 -> 90
        assert _decode_signal(0x72) == 90
        # 0xe8 & 0x1f = 0x08 = 8 -> 40
        assert _decode_signal(0xE8) == 40


# ---------------------------------------------------------------------------
# _decode_battery
# ---------------------------------------------------------------------------


class TestDecodeBattery:
    def test_full_battery(self):
        assert _decode_battery(0x0A) == 100  # 10 * 10

    def test_ninety_percent(self):
        assert _decode_battery(0x09) == 90

    def test_fifty_percent(self):
        assert _decode_battery(0x05) == 50

    def test_zero(self):
        assert _decode_battery(0x00) == 0

    def test_special_no_change(self):
        assert _decode_battery(0x0B) is None

    def test_special_no_measurement(self):
        assert _decode_battery(0x0E) is None

    def test_special_max(self):
        assert _decode_battery(0x0F) is None

    def test_clamp_to_100(self):
        # Lower nibble 0x0a = 10 -> 100, already max
        assert _decode_battery(0x0A) == 100
