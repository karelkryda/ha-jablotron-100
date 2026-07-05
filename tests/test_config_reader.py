"""
Tests for custom_components.jablotron_local.config_reader.

Test vectors are synthetic binary data that exercises the same parsing
patterns as a real JA-100+ panel config (section names, device entries,
section assignments, edge cases).
"""

from pathlib import Path

import pytest

from custom_components.jablotron_local.config_reader import (
    ConfigReadError,
    PanelConfig,
    _find_section_byte,
    _has_flexi_cfg_label,
    _is_valid_name,
    _parse_device_entries,
    _parse_section_names,
    read_panel_config,
)

# ---------------------------------------------------------------------------
# Test data builders
# ---------------------------------------------------------------------------


def _build_section_record(section_num: int, name: str) -> bytes:
    """Build a section name record: 06 81 [num] 85 00 [0xa0+len] [name]."""
    encoded = name.encode("utf-8")
    return (
        b"\x06\x81"
        + bytes([section_num])
        + b"\x85\x00"
        + bytes([0xA0 + len(encoded)])
        + encoded
    )


def _build_device_record(section: int, device_type: int, name: str) -> bytes:
    """Build a device entry record with proper tag structure."""
    encoded = name.encode("utf-8")
    return (
        b"\x01\x04"
        b"\x02"
        + bytes([section])
        + b"\x03"
        + bytes([device_type])
        + b"\x04\xd0\xff"
        + b"\x05\x94\x00\x00\x00\x00"
        + b"\x06"
        + bytes([0xA0 + len(encoded)])
        + encoded
        + b"\x07\xa0"
    )


def _build_config_blob(
    section_records: list[bytes],
    device_records: list[bytes],
) -> bytes:
    """Build a full-size decrypted config blob with sections and devices."""
    config = bytearray(1921 * 512)

    # Place section records at offset 496+.
    offset = 496
    for rec in section_records:
        config[offset : offset + len(rec)] = rec
        offset += len(rec)
        # Padding between sections (matches real panel pattern).
        padding = b"\x01\x00\x02\x00\x03\x00\x04\xa0"
        config[offset : offset + len(padding)] = padding
        offset += len(padding)

    # Place device records at offset 1400+.
    offset = 1400
    for rec in device_records:
        # Inter-record bytes (device num echo, RF address - filler).
        filler = b"\x09\x81\x00\x88\x00\xce\x10\x00\x00\x00"
        config[offset : offset + len(filler)] = filler
        offset += len(filler)
        config[offset : offset + len(rec)] = rec
        offset += len(rec)

    return bytes(config)


# ---------------------------------------------------------------------------
# Synthetic config data
# ---------------------------------------------------------------------------

# Two sections: section 1 = "House", section 3 = "Garage"
_SECTION_RECORDS = [
    _build_section_record(0x00, "House"),
    _build_section_record(0x02, "Garage"),
]

# Devices spanning multiple sections, types, and name patterns.
_DEVICE_RECORDS = [
    _build_device_record(0x00, 0x0E, "Central Unit"),
    _build_device_record(0x00, 0x10, "Radio Module"),
    _build_device_record(0x00, 0x00, "Motion Living"),
    _build_device_record(0x00, 0x00, "Motion Bedroom"),
    _build_device_record(0x02, 0x0D, "Siren Outdoor"),
    _build_device_record(0x02, 0x00, "Motion Garage"),
    _build_device_record(0x01, 0x00, "Shock Sensor"),
    _build_device_record(0x00, 0x2D, "Flood Kitchen"),
    _build_device_record(0x00, 0x03, "Smoke Hallway"),
]

_CONFIG_DATA = _build_config_blob(_SECTION_RECORDS, _DEVICE_RECORDS)


# Minimal data for unit tests.
_SECTION_NAMES_DATA = (
    b"\x00" * 496
    + _build_section_record(0x00, "House")
    + b"\x01\x00\x02\x00\x03\x00\x04\xa0"
    + _build_section_record(0x02, "Garage")
    + b"\x01\x00\x02\x00\x03\x00\x04\xa0"
    + b"\x00" * 500
)

_DEVICE_ENTRIES_DATA = (
    b"\x00" * 1400
    + _build_device_record(0x00, 0x10, "radio")
    + b"\x09\x81\x02\x88\x00\xce\x10\x19\x63\x6b"
    + _build_device_record(0x00, 0x00, "PIR living")
    + b"\x09\x81\x03\x88\x00\xce\x10\x32\x81\x5c"
    + _build_device_record(0x02, 0x0D, "Siren outdoor")
    + b"\x00" * 500
)


# ---------------------------------------------------------------------------
# _parse_section_names
# ---------------------------------------------------------------------------


class TestParseSectionNames:
    def test_parses_two_sections(self):
        names = _parse_section_names(_SECTION_NAMES_DATA)
        assert names == {1: "House", 3: "Garage"}

    def test_section_numbers_from_record_not_order(self):
        names = _parse_section_names(_SECTION_NAMES_DATA)
        assert 2 not in names

    def test_empty_data_returns_empty(self):
        names = _parse_section_names(b"\x00" * 700)
        assert names == {}

    def test_data_too_short(self):
        names = _parse_section_names(b"\x00" * 100)
        assert names == {}

    def test_single_section(self):
        data = b"\x00" * 496 + _build_section_record(0x01, "Basement") + b"\x00" * 500
        names = _parse_section_names(data)
        assert names == {2: "Basement"}

    def test_utf8_section_name(self):
        data = b"\x00" * 496 + _build_section_record(0x00, "Dům") + b"\x00" * 500
        names = _parse_section_names(data)
        assert names == {1: "Dům"}


# ---------------------------------------------------------------------------
# _parse_device_entries
# ---------------------------------------------------------------------------


class TestParseDeviceEntries:
    def test_parses_three_devices(self):
        devices = _parse_device_entries(_DEVICE_ENTRIES_DATA)
        assert len(devices) == 3

    def test_device_names(self):
        devices = _parse_device_entries(_DEVICE_ENTRIES_DATA)
        assert devices[0].name == "radio"
        assert devices[1].name == "PIR living"
        assert devices[2].name == "Siren outdoor"

    def test_device_positions_sequential(self):
        devices = _parse_device_entries(_DEVICE_ENTRIES_DATA)
        assert devices[0].position == 0
        assert devices[1].position == 1
        assert devices[2].position == 2

    def test_section_assignment(self):
        devices = _parse_device_entries(_DEVICE_ENTRIES_DATA)
        assert devices[0].section == 1
        assert devices[1].section == 1
        assert devices[2].section == 3

    def test_empty_data_returns_empty(self):
        devices = _parse_device_entries(b"\x00" * 2000)
        assert devices == []

    def test_rejects_invalid_utf8_names(self):
        data = (
            b"\x00" * 1400
            + b"\x01\x04\x02\x00\x03\x00\x04\xd0\xff"
            + b"\x05\x94\x00\x00\x00\x00"
            + b"\x06\xa3\xff\xfe\xfd\x07\xa0"
            + b"\x00" * 500
        )
        devices = _parse_device_entries(data)
        assert devices == []

    def test_requires_delimiter_after_name(self):
        data = (
            b"\x00" * 1400
            + _build_device_record(0x00, 0x00, "test")[:-2]
            + b"\x00\xa0"  # wrong delimiter (0x00 instead of 0x07)
            + b"\x00" * 500
        )
        devices = _parse_device_entries(data)
        assert devices == []


# ---------------------------------------------------------------------------
# _find_section_byte
# ---------------------------------------------------------------------------


class TestFindSectionByte:
    def test_finds_section_in_standard_record(self):
        # 01 04 02 [section=0x02] 03 0d 04 d0 ff ... 06 name
        data = (
            b"\x01\x04\x02\x02\x03\x0d\x04\xd0\xff"
            b"\x05\x94\x00\x00\x00\x00"
            b"\x06\xa5radio\x07"
        )
        assert _find_section_byte(data, 15) == 3

    def test_finds_section_zero(self):
        data = (
            b"\x01\x04\x02\x00\x03\x10\x04\xd0\xff"
            b"\x05\x94\x00\x00\x00\x00"
            b"\x06\xa5radio\x07"
        )
        assert _find_section_byte(data, 15) == 1

    def test_defaults_to_1_when_not_found(self):
        data = b"\x00" * 60 + b"\x06\xa5radio\x07"
        assert _find_section_byte(data, 60) == 1

    def test_handles_ambiguous_section_value_02(self):
        # Section value is 0x02, same as the section tag byte.
        # Pattern 02 02 03 YY 04 must resolve correctly.
        data = (
            b"\x01\x44\x02\x02\x03\x03\x04\xd0\xff"
            b"\x05\x94\x00\x00\x00\x00"
            b"\x06\xa5smoke\x07"
        )
        assert _find_section_byte(data, 15) == 3


# ---------------------------------------------------------------------------
# _is_valid_name
# ---------------------------------------------------------------------------


class TestIsValidName:
    def test_ascii_name(self):
        assert _is_valid_name("Motion sensor") is True

    def test_utf8_name(self):
        assert _is_valid_name("Garáž") is True

    def test_rejects_replacement_character(self):
        assert _is_valid_name("!S\ufffd3") is False

    def test_rejects_control_characters(self):
        assert _is_valid_name("bad\x00name") is False

    def test_empty_string(self):
        assert _is_valid_name("") is True


# ---------------------------------------------------------------------------
# _has_flexi_cfg_label
# ---------------------------------------------------------------------------


class TestHasFlexiCfgLabel:
    def test_identifies_flexi_cfg(self, tmp_path: Path):
        vbr = bytearray(512)
        vbr[0x2B : 0x2B + 11] = b"FLEXI_CFG  "
        image = b"\x00" * 512 + bytes(vbr)
        device = tmp_path / "fake_disk"
        device.write_bytes(image)
        assert _has_flexi_cfg_label(str(device)) is True

    def test_rejects_wrong_label(self, tmp_path: Path):
        vbr = bytearray(512)
        vbr[0x2B : 0x2B + 11] = b"FLEXI_LOG  "
        image = b"\x00" * 512 + bytes(vbr)
        device = tmp_path / "fake_disk"
        device.write_bytes(image)
        assert _has_flexi_cfg_label(str(device)) is False

    def test_rejects_nonexistent_file(self):
        assert _has_flexi_cfg_label("/dev/nonexistent_xyz") is False

    def test_rejects_short_file(self, tmp_path: Path):
        device = tmp_path / "short"
        device.write_bytes(b"\x00" * 100)
        assert _has_flexi_cfg_label(str(device)) is False


# ---------------------------------------------------------------------------
# read_panel_config (integration of all parsers)
# ---------------------------------------------------------------------------


class TestReadPanelConfig:
    def _make_device_image(self, tmp_path: Path, decrypted_config: bytes) -> str:
        """Create a fake block device with encrypted config."""
        encrypted = bytes(b ^ 0xFF for b in decrypted_config)
        image = b"\x00" * (35 * 512) + encrypted
        device = tmp_path / "fake_disk"
        device.write_bytes(image)
        return str(device)

    def test_reads_and_parses_config(self, tmp_path: Path):
        path = self._make_device_image(tmp_path, _CONFIG_DATA)
        result = read_panel_config(path)

        assert isinstance(result, PanelConfig)
        assert result.section_names == {1: "House", 3: "Garage"}
        assert len(result.devices) == 9
        assert result.devices[0].name == "Central Unit"
        assert result.devices[0].section == 1
        assert result.devices[4].name == "Siren Outdoor"
        assert result.devices[4].section == 3
        assert result.devices[6].name == "Shock Sensor"
        assert result.devices[6].section == 2

    def test_xor_decryption(self, tmp_path: Path):
        # Verify the XOR 0xFF decryption works end-to-end.
        path = self._make_device_image(tmp_path, _CONFIG_DATA)
        # Read the raw file and verify it's encrypted.
        raw = Path(path).read_bytes()
        encrypted_byte = raw[35 * 512 + 496]
        decrypted_byte = _CONFIG_DATA[496]
        assert encrypted_byte == decrypted_byte ^ 0xFF

        result = read_panel_config(path)
        assert result.section_names[1] == "House"

    def test_raises_on_permission_error(self, tmp_path: Path):
        device = tmp_path / "no_access"
        device.write_bytes(b"\x00" * 100)
        device.chmod(0o000)
        try:
            with pytest.raises(ConfigReadError, match="Permission denied"):
                read_panel_config(str(device))
        finally:
            device.chmod(0o644)

    def test_raises_on_nonexistent_file(self):
        with pytest.raises(ConfigReadError, match="Cannot read"):
            read_panel_config("/dev/nonexistent_xyz_device")

    def test_raises_on_short_read(self, tmp_path: Path):
        device = tmp_path / "small"
        device.write_bytes(b"\x00" * (35 * 512 + 100))
        with pytest.raises(ConfigReadError, match="Short read"):
            read_panel_config(str(device))

    def test_no_garbage_names(self, tmp_path: Path):
        path = self._make_device_image(tmp_path, _CONFIG_DATA)
        result = read_panel_config(path)
        for device in result.devices:
            assert "\ufffd" not in device.name
            assert all(c.isprintable() or c == " " for c in device.name)
