"""
Tests for custom_components.jablotron_local.protocol.

Test vectors are derived from verified USB captures of JA-Link
communicating with a JA-103K panel.
"""

import pytest

from custom_components.jablotron_local.protocol import (
    REPORT_SIZE,
    ArmMode,
    CodeError,
    Command,
    DeviceActivity,
    Packet,
    PacketType,
    ReportTooLongError,
    SectionPrimaryState,
    SectionSecondaryState,
    SectionState,
    SysInfoType,
    SystemInfo,
    UiControl,
    UiStatusReason,
    cmd_enable_device_states,
    cmd_get_sections_and_pg,
    cmd_get_system_info,
    cmd_heartbeat,
    decode_devices_states,
    decode_report,
    decode_sections,
    decode_system_info,
    decode_ui_status,
    encode_report,
    ui_authorisation_code,
    ui_authorisation_end,
    ui_modify_section,
    ui_toggle_pg_output,
)

# ---------------------------------------------------------------------------
# encode_report / decode_report
# ---------------------------------------------------------------------------


class TestEncodeReport:
    def test_single_packet_pads_to_64(self):
        pkt = Packet(PacketType.COMMAND, bytes([Command.HEARTBEAT]))
        report = encode_report(pkt)
        assert len(report) == REPORT_SIZE
        assert report[:3] == bytes([0x52, 0x01, 0x02])
        assert report[3:] == b"\x00" * (REPORT_SIZE - 3)

    def test_multiple_packets(self):
        p1 = Packet(PacketType.COMMAND, bytes([Command.GET_SECTIONS_AND_PG]))
        p2 = Packet(PacketType.COMMAND, bytes([Command.ENABLE_DEV_STATES, 0x05]))
        report = encode_report(p1, p2)
        assert len(report) == REPORT_SIZE
        assert report[:3] == bytes([0x52, 0x01, 0x0E])
        assert report[3:6] == bytes([0x52, 0x02, 0x13])
        assert report[6] == 0x05

    def test_too_long_raises(self):
        # Each Packet(type, 1-byte data) encodes to 3 bytes (type + len + data)
        # 21 * 3 = 63 bytes -> fine; add a 22nd to tip over to 66 bytes
        pkts = [Packet(0x52, b"\x00") for _ in range(21)]
        encode_report(*pkts)  # 63 bytes - fine
        pkts.append(Packet(0x52, b"\x00\x00"))  # 63 + 4 = 67 bytes -> over
        with pytest.raises(ReportTooLongError):
            encode_report(*pkts)

    def test_empty_data_packet(self):
        pkt = Packet(0x52, b"")
        report = encode_report(pkt)
        assert report[0] == 0x52
        assert report[1] == 0x00
        assert report[2] == 0x00  # padding


class TestDecodeReport:
    def test_heartbeat_report(self):
        report = encode_report(cmd_heartbeat())
        packets = decode_report(report)
        assert len(packets) == 1
        assert packets[0].type == PacketType.COMMAND
        assert packets[0].data == bytes([Command.HEARTBEAT])

    def test_stops_at_zero_type(self):
        # Manually construct report with two packets then padding zeros
        report = bytes([0x52, 0x01, 0x02, 0x51, 0x01, 0xFF]) + b"\x00" * 58
        packets = decode_report(report)
        assert len(packets) == 2

    def test_roundtrip_multiple(self):
        originals = [
            cmd_heartbeat(),
            cmd_get_sections_and_pg(),
            cmd_enable_device_states(5),
        ]
        packets = decode_report(encode_report(*originals))
        assert len(packets) == len(originals)
        for orig, decoded in zip(originals, packets, strict=True):
            assert decoded.type == orig.type
            assert decoded.data == orig.data

    def test_truncated_payload_does_not_crash(self):
        # Declare length 10 but only 3 bytes follow
        bad_report = bytes([0x52, 0x0A, 0x01, 0x02, 0x03]) + b"\x00" * 59
        packets = decode_report(bad_report)
        # Should stop gracefully
        assert isinstance(packets, list)

    def test_all_zeros_returns_empty(self):
        assert decode_report(b"\x00" * REPORT_SIZE) == []


# ---------------------------------------------------------------------------
# Unauthenticated outbound builders
# ---------------------------------------------------------------------------


class TestCmdBuilders:
    def test_heartbeat(self):
        # 52 01 02
        pkt = cmd_heartbeat()
        assert pkt.type == PacketType.COMMAND
        assert pkt.data == bytes([0x02])

    def test_get_sections_and_pg(self):
        # 52 01 0e
        pkt = cmd_get_sections_and_pg()
        assert pkt.type == PacketType.COMMAND
        assert pkt.data == bytes([0x0E])

    def test_enable_device_states_default(self):
        # 52 02 13 05
        pkt = cmd_enable_device_states()
        assert pkt.type == PacketType.COMMAND
        assert pkt.data == bytes([0x13, 0x05])

    def test_enable_device_states_custom_minutes(self):
        pkt = cmd_enable_device_states(10)
        assert pkt.data == bytes([0x13, 0x0A])

    def test_get_system_info_model(self):
        # 30 01 02
        pkt = cmd_get_system_info(SysInfoType.MODEL)
        assert pkt.type == PacketType.GET_SYS_INFO
        assert pkt.data == bytes([0x02])

    def test_get_system_info_firmware(self):
        # 0x08 (not 0x09 - corrected vs kukulich's swapped labels)
        pkt = cmd_get_system_info(SysInfoType.FIRMWARE)
        assert pkt.data == bytes([0x08])

    def test_get_system_info_hardware(self):
        pkt = cmd_get_system_info(SysInfoType.HARDWARE)
        assert pkt.data == bytes([0x09])


# ---------------------------------------------------------------------------
# Authenticated outbound builders (pcap-verified vectors)
# ---------------------------------------------------------------------------


class TestAuthBuilders:
    def test_auth_end(self):
        # 80 01 01
        pkt = ui_authorisation_end()
        assert pkt.type == PacketType.UI_CONTROL
        assert pkt.data == bytes([UiControl.AUTHORISATION_END])

    def test_auth_code_user999_pin1234(self):
        """
        Verified capture vector (successful login):
        OUT  UI_CONTROL.AUTH_CODE  03 39 39 39 31 32 33 34
        = subtype 0x03 + b'9991234'
        """
        pkt = ui_authorisation_code("999", "1234")
        assert pkt.type == PacketType.UI_CONTROL
        assert pkt.data == bytes([0x03]) + b"9991234"
        assert pkt.data.hex() == "03" + "39393931323334"

    def test_auth_code_user001_pin1234(self):
        """
        Verified capture vector (wrong code):
        OUT  UI_CONTROL.AUTH_CODE  03 30 30 31 31 32 33 34
        = subtype 0x03 + b'0011234'
        """
        pkt = ui_authorisation_code("001", "1234")
        assert pkt.type == PacketType.UI_CONTROL
        assert pkt.data == bytes([0x03]) + b"0011234"
        assert pkt.data.hex() == "03" + "30303131323334"

    def test_auth_code_too_short_raises(self):
        with pytest.raises(CodeError):
            ui_authorisation_code("999", "123")

    def test_auth_code_too_long_raises(self):
        with pytest.raises(CodeError):
            ui_authorisation_code("999", "12345678901")

    def test_auth_code_min_length(self):
        pkt = ui_authorisation_code("999", "1234")
        assert len(pkt.data) == 1 + 3 + 4  # subtype + prefix + code

    def test_auth_code_max_length(self):
        pkt = ui_authorisation_code("999", "1234567890")
        assert len(pkt.data) == 1 + 3 + 10

    def test_modify_section_arm_away_section2(self):
        """
        Verified capture vector:
        OUT  UI_CONTROL.MODIFY_SECTION  0d a1
        ArmMode.ARM_AWAY (0x9f) + 2 = 0xa1
        """
        pkt = ui_modify_section(2, ArmMode.ARM_AWAY)
        assert pkt.type == PacketType.UI_CONTROL
        assert pkt.data == bytes([0x0D, 0xA1])

    def test_modify_section_disarm_section2(self):
        """
        Verified capture vector:
        OUT  UI_CONTROL.MODIFY_SECTION  0d 91
        ArmMode.DISARM (0x8f) + 2 = 0x91
        """
        pkt = ui_modify_section(2, ArmMode.DISARM)
        assert pkt.type == PacketType.UI_CONTROL
        assert pkt.data == bytes([0x0D, 0x91])

    def test_modify_section_arm_away_section1(self):
        # Formula: 0x9f + 1 = 0xa0  (unverified by pcap)
        pkt = ui_modify_section(1, ArmMode.ARM_AWAY)
        assert pkt.data == bytes([0x0D, 0xA0])

    def test_modify_section_disarm_section1(self):
        # Formula: 0x8f + 1 = 0x90  (unverified by pcap)
        pkt = ui_modify_section(1, ArmMode.DISARM)
        assert pkt.data == bytes([0x0D, 0x90])

    def test_modify_section_arm_away_section3(self):
        # Formula: 0x9f + 3 = 0xa2  (unverified by pcap)
        pkt = ui_modify_section(3, ArmMode.ARM_AWAY)
        assert pkt.data == bytes([0x0D, 0xA2])

    def test_modify_section_disarm_section3(self):
        # Formula: 0x8f + 3 = 0x92  (unverified by pcap)
        pkt = ui_modify_section(3, ArmMode.DISARM)
        assert pkt.data == bytes([0x0D, 0x92])

    def test_toggle_pg_output(self):
        pkt = ui_toggle_pg_output(1)
        assert pkt.type == PacketType.UI_CONTROL
        assert pkt.data == bytes([UiControl.TOGGLE_PG_OUTPUT, 0x01])


# ---------------------------------------------------------------------------
# Inbound decoders
# ---------------------------------------------------------------------------


class TestDecodeSections:
    # Verified section state vectors from USB capture
    # Sections payload after arm: 01 00 03 00 01 00 07 00 ... 00 94
    # Section 1=DISARMED, 2=ARMED_FULL, 3=DISARMED, rest=OFF

    def test_parse_armed_state(self):
        """
        After MODIFY_SECTION arm:
        51 data: 01 00 03 00 01 00 07 00 07 00 ... 00 94
        sec1=DISARMED(1), sec2=ARMED_FULL(3), sec3=DISARMED(1)
        """
        data = bytes(
            [
                0x01,
                0x00,  # sec 1: DISARMED
                0x03,
                0x00,  # sec 2: ARMED_FULL
                0x01,
                0x00,  # sec 3: DISARMED
                0x07,
                0x00,  # sec 4: OFF
                0x07,
                0x00,  # sec 5: OFF
                0x07,
                0x00,
                0x07,
                0x00,
                0x07,
                0x00,
                0x07,
                0x00,
                0x07,
                0x00,
                0x07,
                0x00,
                0x07,
                0x00,
                0x07,
                0x00,
                0x07,
                0x00,
                0x07,
                0x00,
                0x07,
                0x00,
                0x00,
                0x94,  # trailer
            ]
        )
        states = decode_sections(data)
        assert len(states) == 3
        assert states[0] == SectionState(
            1, SectionPrimaryState.DISARMED, SectionSecondaryState.NORMAL, 0
        )
        assert states[1] == SectionState(
            2, SectionPrimaryState.ARMED_FULL, SectionSecondaryState.NORMAL, 0
        )
        assert states[2] == SectionState(
            3, SectionPrimaryState.DISARMED, SectionSecondaryState.NORMAL, 0
        )

    def test_parse_all_disarmed(self):
        """
        Initial / after disarm:
        sec1=1, sec2=1, sec3=1, rest=OFF
        """
        data = bytes(
            [
                0x01,
                0x00,
                0x01,
                0x00,
                0x01,
                0x00,
                0x07,
                0x00,
                0x07,
                0x00,
                0x07,
                0x00,
                0x07,
                0x00,
                0x07,
                0x00,
                0x07,
                0x00,
                0x07,
                0x00,
                0x07,
                0x00,
                0x07,
                0x00,
                0x07,
                0x00,
                0x07,
                0x00,
                0x07,
                0x00,
                0x00,
                0x94,
            ]
        )
        states = decode_sections(data)
        numbers = [s.number for s in states]
        assert numbers == [1, 2, 3]
        assert all(s.primary == SectionPrimaryState.DISARMED for s in states)

    def test_off_slots_excluded(self):
        data = bytes([0x07, 0x00] * 16 + [0x00, 0x94])
        assert decode_sections(data) == []

    def test_flags_preserved(self):
        data = bytes([0x01, 0xAB]) + bytes([0x07, 0x00] * 15) + bytes([0x00, 0x94])
        states = decode_sections(data)
        assert states[0].flags == 0xAB

    def test_short_data_does_not_crash(self):
        assert decode_sections(b"\x01") == []
        assert decode_sections(b"") == []

    def test_arming_bit7_masked_to_armed_full(self):
        """
        During exit delay the panel sends 0x83 (0x80 | ARMED_FULL).
        decode_sections must mask bit 7 and set secondary=ARMING.
        """
        data = bytes([0x83, 0x00]) + bytes([0x07, 0x00] * 15) + bytes([0x00, 0x94])
        states = decode_sections(data)
        assert len(states) == 1
        assert states[0] == SectionState(
            1, SectionPrimaryState.ARMED_FULL, SectionSecondaryState.ARMING, 0
        )

    def test_arming_bit7_masked_to_armed_partial(self):
        """0x82 = 0x80 | ARMED_PARTIAL → arming towards partial."""
        data = bytes([0x82, 0x00]) + bytes([0x07, 0x00] * 15) + bytes([0x00, 0x94])
        states = decode_sections(data)
        assert len(states) == 1
        assert states[0] == SectionState(
            1, SectionPrimaryState.ARMED_PARTIAL, SectionSecondaryState.ARMING, 0
        )

    def test_arming_mixed_with_normal(self):
        """
        Real capture: section 1 arming (0x83), section 2 disarmed (0x01),
        section 3 armed (0x03).
        """
        data = bytes(
            [
                0x83,
                0x00,  # sec 1: arming towards ARMED_FULL
                0x01,
                0x00,  # sec 2: DISARMED
                0x03,
                0x00,  # sec 3: ARMED_FULL
            ]
            + [0x07, 0x00] * 13
            + [0x00, 0x90]
        )
        states = decode_sections(data)
        assert len(states) == 3
        assert states[0] == SectionState(
            1, SectionPrimaryState.ARMED_FULL, SectionSecondaryState.ARMING, 0
        )
        assert states[1] == SectionState(
            2, SectionPrimaryState.DISARMED, SectionSecondaryState.NORMAL, 0
        )
        assert states[2] == SectionState(
            3, SectionPrimaryState.ARMED_FULL, SectionSecondaryState.NORMAL, 0
        )

    def test_arming_bit7_with_off_slot_skipped(self):
        """0x87 = 0x80 | OFF (7) should still be skipped."""
        data = bytes([0x87, 0x00]) + bytes([0x07, 0x00] * 15) + bytes([0x00, 0x94])
        assert decode_sections(data) == []


class TestDecodeDevicesStates:
    def test_no_active_devices(self):
        # All zeros
        result = decode_devices_states(bytes([0x05, 0x00, 0x00, 0x00, 0x00]))
        assert result.active == frozenset()

    def test_device_2_active(self):
        # skip byte 0, then LE bitmap; device 2 = bit 2 of byte 0
        # bytes: [skip, 0b00000100] -> device 2 active
        result = decode_devices_states(bytes([0x00, 0x04]))
        assert 2 in result.active

    def test_device_1_is_bit_1(self):
        result = decode_devices_states(bytes([0x00, 0x02]))
        assert 1 in result.active

    def test_multiple_devices(self):
        # devices 1, 3, 5 = bits 1, 3, 5 = 0b00101010 = 0x2a
        result = decode_devices_states(bytes([0x00, 0x2A]))
        assert result.active == frozenset({1, 3, 5})

    def test_too_short_returns_empty(self):
        assert decode_devices_states(b"") == DeviceActivity(active=frozenset())
        assert decode_devices_states(b"\x00") == DeviceActivity(active=frozenset())

    def test_little_endian_multi_byte(self):
        # device 9 = bit 9; byte 0 skip, byte 1 = bits 1-8, byte 2 bit 1 = device 9
        result = decode_devices_states(bytes([0x00, 0x00, 0x02]))
        assert 9 in result.active


class TestDecodeSystemInfo:
    def test_model_ja103k(self):
        """
        Example: type=0x02(MODEL), value='JA-103K'
        type=0x02(MODEL), value='JA-103K'
        """
        data = bytes([0x02]) + b"JA-103K"
        result = decode_system_info(data)
        assert result == SystemInfo(SysInfoType.MODEL, "JA-103K")

    def test_firmware(self):
        """
        Example: type=0x08(FIRMWARE), value='MD6112.07.0'
        type=0x08(FIRMWARE), value='MD6112.07.0'
        """
        data = bytes([0x08]) + b"MD6112.07.0"
        result = decode_system_info(data)
        assert result == SystemInfo(SysInfoType.FIRMWARE, "MD6112.07.0")

    def test_hardware(self):
        data = bytes([0x09]) + b"MD15005"
        result = decode_system_info(data)
        assert result == SystemInfo(SysInfoType.HARDWARE, "MD15005")

    def test_mac_formatted(self):
        """Example: type=0x0c(MAC), value='00:11:22:33:44:55'"""
        data = bytes([0x0C, 0x00, 0x11, 0x22, 0x33, 0x44, 0x55])
        result = decode_system_info(data)
        assert result == SystemInfo(SysInfoType.MAC, "00:11:22:33:44:55")

    def test_nul_terminated_string(self):
        data = bytes([0x0B]) + b"alarm\x00\x00\x00\x00\x00"
        result = decode_system_info(data)
        assert result is not None
        assert result.value == "alarm"

    def test_unknown_type_returns_none(self):
        assert decode_system_info(bytes([0xFF, 0x01])) is None

    def test_empty_returns_none(self):
        assert decode_system_info(b"") is None


class TestDecodeUiStatus:
    def test_wrong_code(self):
        """
        Pcap vector: IN UI_CONTROL.STATUS[WRONG_CODE] 1b 03
        data passed is the UI_CONTROL DATA field = [0x1b, 0x03]
        """
        result = decode_ui_status(bytes([0x1B, 0x03]))
        assert result is not None
        assert result.reason == UiStatusReason.WRONG_CODE

    def test_no_session(self):
        result = decode_ui_status(bytes([0x1B, 0x06]))
        assert result is not None
        assert result.reason == UiStatusReason.NO_SESSION

    def test_raw_preserved(self):
        data = bytes([0x1B, 0x03])
        result = decode_ui_status(data)
        assert result is not None
        assert result.raw == data

    def test_too_short_returns_none(self):
        assert decode_ui_status(b"\x1b") is None
        assert decode_ui_status(b"") is None
