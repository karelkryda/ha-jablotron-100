"""
Panel configuration reader via the FLEXI_CFG mass storage volume.

The Jablotron JA-100+ panel exports its configuration (device names,
section names, section assignments) to the FLEXI_CFG volume on demand.
The data is "encrypted" with simple bitwise NOT (XOR 0xFF).

The export requires an authenticated HID session with service/installer
permissions. The sequence is:

1. Authenticate via HID with service PIN.
2. Send export trigger (``80 01 0f``).
3. Wait for export done (``80 01 12``, ~900ms).
4. Read LBA 35-1955 from the FLEXI_CFG block device.
5. Decrypt with XOR 0xFF.
6. Logout.

This module handles steps 4-5 (discovery + read + parse). Steps 1-3
and 6 are handled by :meth:`client.JablotronClient.export_config`.

All functions are blocking and MUST be called via
``hass.async_add_executor_job``.
"""

from dataclasses import dataclass
from pathlib import Path

from .const import LOGGER, USB_PRODUCT_ID, USB_VENDOR_ID

# Sector geometry for config data on FLEXI_CFG.
_SECTOR_SIZE: int = 512
_CONFIG_LBA_START: int = 35
_CONFIG_LBA_END: int = 1955
_CONFIG_SECTORS: int = _CONFIG_LBA_END - _CONFIG_LBA_START + 1

# Name tag encoding.
_NAME_TAG: int = 0x06
_NAME_LEN_BASE: int = 0xA0
_NAME_DELIMITER: int = 0x07

# Approximate byte offsets within decrypted config.
_SECTION_NAMES_START: int = 400
_SECTION_NAMES_END: int = 600
_DEVICE_ENTRIES_START: int = 1400

# Maximum reasonable name length (UTF-8 bytes).
_MAX_NAME_LENGTH: int = 40

# Section assignment tag and max valid section byte.
_SECTION_TAG: int = 0x02
_TYPE_TAG: int = 0x03
_EXTRA_TAG: int = 0x04
_MAX_SECTION_BYTE: int = 0x0F

# Section name record structure subtags.
_SECTION_RECORD_SUBTAG: int = 0x81
_SECTION_NAME_SUBTAG: int = 0x85

# RF byte0 range for bus/wired devices (0x10-0x1f).
_RF_BUS_MIN: int = 0x10
_RF_BUS_MAX: int = 0x1F

# RF address marker pattern: 88 00 ce [rf_byte0]
_RF_MARKER = bytes([0x88, 0x00, 0xCE])

# FLEXI_CFG is identified by its FAT16 volume label in the VBR.
# The panel's partition table starts at LBA 1, so the VBR is at
# byte offset 512 on the whole-disk device.
_VBR_OFFSET: int = 1 * _SECTOR_SIZE
_FAT16_VOLUME_LABEL_OFFSET: int = 0x2B
_FAT16_VOLUME_LABEL_LENGTH: int = 11
_FLEXI_CFG_LABEL: bytes = b"FLEXI_CFG  "  # 11 bytes, space-padded

SYSFS_BLOCK = Path("/sys/class/block")


@dataclass(frozen=True, slots=True)
class DeviceEntry:
    """
    A device parsed from the panel configuration.

    Attributes:
        position: 0-based device number (matches HID bitmap bit position).
        name: Human-readable device name from the panel config.
        section: Section number (1-based) the device belongs to.
        rf_byte0: First byte of the RF address (determines bus vs wireless).

    """

    position: int
    name: str
    section: int
    rf_byte0: int

    @property
    def is_bus_device(self) -> bool:
        """True if this is a wired/bus device (rf_byte0 in 0x10-0x1f)."""
        return _RF_BUS_MIN <= self.rf_byte0 <= _RF_BUS_MAX


@dataclass(frozen=True, slots=True)
class PanelConfig:
    """
    Parsed panel configuration from the FLEXI_CFG volume.

    Attributes:
        section_names: Mapping of section number (1-based) to name.
        devices: List of device entries in position order.

    """

    section_names: dict[int, str]
    devices: list[DeviceEntry]


class ConfigReadError(Exception):
    """Raised when the panel config cannot be read or parsed."""

    def __init__(self, detail: str) -> None:
        """Store the failure detail."""
        self.detail = detail
        super().__init__(f"Config read failed: {detail}")


def find_flexi_cfg_device(hidraw_path: str) -> str | None:
    """
    Find the FLEXI_CFG block device that is a sibling of the hidraw device.

    Both the hidraw and mass-storage interfaces share the same USB parent
    device. We walk sysfs from the hidraw device up to the USB device,
    then search ``/sys/class/block/`` for block devices under the same
    USB parent with the FLEXI_CFG volume label.

    Args:
        hidraw_path: The hidraw device path, e.g. ``/dev/hidraw3``.

    Returns:
        The block device path (e.g. ``/dev/sda``) or ``None`` if not found.

    """
    hidraw_name = Path(hidraw_path).name
    hidraw_sysfs = Path("/sys/class/hidraw") / hidraw_name / "device"

    if not hidraw_sysfs.is_dir():
        LOGGER.debug("sysfs path not found for %s", hidraw_path)
        return None

    # Walk up from the HID interface to the USB device.
    # hidraw -> hid device -> USB interface -> USB device
    usb_device = _find_usb_device(hidraw_sysfs)
    if usb_device is None:
        LOGGER.debug("Could not find USB device parent for %s", hidraw_path)
        return None

    usb_device_real = usb_device.resolve()
    LOGGER.debug("USB device for %s: %s", hidraw_path, usb_device_real)

    # Search block devices for one under the same USB device.
    if not SYSFS_BLOCK.is_dir():
        return None

    candidates: list[str] = []
    for block_entry in sorted(SYSFS_BLOCK.iterdir()):
        # Skip partitions (e.g. sda1) - we want the whole-disk device.
        if any(c.isdigit() for c in block_entry.name):
            continue

        device_link = block_entry / "device"
        if not device_link.exists():
            continue

        # Walk up from the SCSI device to the USB device.
        block_usb = _find_usb_device_from_block(block_entry)
        if block_usb is None:
            continue

        if block_usb.resolve() == usb_device_real:
            candidates.append(f"/dev/{block_entry.name}")

    if not candidates:
        LOGGER.debug("No sibling block devices found for %s", hidraw_path)
        return None

    # Identify FLEXI_CFG by reading the FAT16 volume label from the VBR.
    for dev_path in candidates:
        if _has_flexi_cfg_label(dev_path):
            LOGGER.debug("FLEXI_CFG block device: %s", dev_path)
            return dev_path

    LOGGER.debug(
        "No sibling block device has FLEXI_CFG volume label for %s",
        hidraw_path,
    )
    return None


def _has_flexi_cfg_label(block_device_path: str) -> bool:
    """
    Check if a block device contains the FLEXI_CFG FAT16 volume label.

    Reads the Volume Boot Record (at LBA 1 on the whole-disk device)
    and checks the 11-byte volume label field at offset 0x2B.

    Args:
        block_device_path: Path to the whole-disk block device.

    Returns:
        ``True`` if the volume label matches "FLEXI_CFG".

    """
    try:
        with Path(block_device_path).open("rb") as f:
            f.seek(_VBR_OFFSET + _FAT16_VOLUME_LABEL_OFFSET)
            label = f.read(_FAT16_VOLUME_LABEL_LENGTH)
    except OSError:
        return False

    return label == _FLEXI_CFG_LABEL


def read_panel_config(block_device_path: str) -> PanelConfig:
    """
    Read and parse the panel configuration from the FLEXI_CFG block device.

    Must be called AFTER the export trigger has completed (``80 01 12``
    received). The data is only available while the authenticated
    session is active.

    Reads raw sectors, decrypts with XOR 0xFF, and parses section names
    and device entries.

    Args:
        block_device_path: Path to the block device, e.g. ``/dev/sda``.

    Returns:
        Parsed :class:`PanelConfig`.

    Raises:
        ConfigReadError: If the block device cannot be read or the
            config cannot be parsed.

    """
    try:
        with Path(block_device_path).open("rb") as f:
            f.seek(_CONFIG_LBA_START * _SECTOR_SIZE)
            encrypted = f.read(_CONFIG_SECTORS * _SECTOR_SIZE)
    except PermissionError as err:
        msg = f"Permission denied reading {block_device_path}"
        raise ConfigReadError(msg) from err
    except OSError as err:
        msg = f"Cannot read {block_device_path}: {err}"
        raise ConfigReadError(msg) from err

    if len(encrypted) < _CONFIG_SECTORS * _SECTOR_SIZE:
        msg = (
            f"Short read from {block_device_path}: "
            f"got {len(encrypted)} bytes, expected {_CONFIG_SECTORS * _SECTOR_SIZE}"
        )
        raise ConfigReadError(msg)

    # Decrypt: XOR 0xFF (bitwise NOT).
    decrypted = bytes(b ^ 0xFF for b in encrypted)

    section_names = _parse_section_names(decrypted)
    devices = _parse_device_entries(decrypted)

    LOGGER.debug(
        "Parsed panel config: %d section names, %d devices",
        len(section_names),
        len(devices),
    )

    return PanelConfig(section_names=section_names, devices=devices)


def _parse_section_names(data: bytes) -> dict[int, str]:
    """
    Parse section names from the decrypted config.

    Section names appear in the range ~400-600 bytes in a tagged
    structure::

        06 81 [section_num] 85 00 [0xa0+name_len] [name_utf8]

    Where ``section_num`` is 0-based (0x00 = section 1, 0x02 = section 3)
    and the name uses the same 0xa0 + length encoding as device names.

    Returns:
        Mapping of section number (1-based) to name.

    """
    names: dict[int, str] = {}
    i = _SECTION_NAMES_START

    while i < _SECTION_NAMES_END and i + 6 < len(data):
        # Match pattern: 06 81 [section_num] 85 00 [0xa0+len]
        if (
            data[i] == _NAME_TAG
            and data[i + 1] == _SECTION_RECORD_SUBTAG
            and data[i + 3] == _SECTION_NAME_SUBTAG
            and data[i + 4] == 0x00
            and data[i + 5] >= _NAME_LEN_BASE
        ):
            section_raw = data[i + 2]
            name_len = data[i + 5] - _NAME_LEN_BASE
            if 1 <= name_len <= _MAX_NAME_LENGTH and i + 6 + name_len <= len(data):
                name = data[i + 6 : i + 6 + name_len].decode("utf-8", errors="replace")
                if any(c.isalpha() for c in name):
                    names[section_raw + 1] = name

                i += 6 + name_len
                continue

        i += 1

    return names


def _parse_device_entries(data: bytes) -> list[DeviceEntry]:
    """
    Parse device entries from the decrypted config.

    Devices appear sequentially starting around offset 1400. Each entry
    has the structure::

        ... 02 [section_byte] 03 [type_byte] 04 ... 06 [0xa0+len] [name] 07 ...

    Position in the list = device number (0-based), matching the HID
    protocol's bitmap bit and device numbering.

    Returns:
        List of :class:`DeviceEntry` in position order.

    """
    devices: list[DeviceEntry] = []
    i = _DEVICE_ENTRIES_START

    while i < len(data) - 5:
        if data[i] == _NAME_TAG:
            len_byte = data[i + 1]
            if len_byte >= _NAME_LEN_BASE:
                name_len = len_byte - _NAME_LEN_BASE
            else:
                i += 1
                continue

            if (
                1 <= name_len <= _MAX_NAME_LENGTH
                and i + 2 + name_len < len(data)
                and data[i + 2 + name_len] == _NAME_DELIMITER
            ):
                name = data[i + 2 : i + 2 + name_len].decode("utf-8", errors="replace")
                if any(c.isalpha() for c in name) and _is_valid_name(name):
                    # Look backwards for section assignment byte.
                    section = _find_section_byte(data, i)
                    rf_byte0 = _find_rf_byte0(data, i)
                    devices.append(
                        DeviceEntry(
                            position=len(devices),
                            name=name,
                            section=section,
                            rf_byte0=rf_byte0,
                        )
                    )

                i += 2 + name_len
                continue

        i += 1

    return devices


def _is_valid_name(name: str) -> bool:
    """
    Check if a parsed name looks like a real device name.

    Rejects garbage strings that result from parsing past the end of
    the device entries region. Valid names contain only printable
    characters and no Unicode replacement characters (which indicate
    invalid UTF-8 bytes in the source).

    Args:
        name: Decoded name string.

    Returns:
        ``True`` if the name is a valid device name.

    """
    return "\ufffd" not in name and all(c.isprintable() or c == " " for c in name)


def _find_section_byte(data: bytes, name_offset: int) -> int:
    """
    Find the section assignment byte preceding a device name.

    The record structure before each name contains the pattern::

        02 [section_byte] 03 [type_byte] 04 ...

    We match the full ``02 XX 03 YY 04`` sequence to avoid ambiguity
    when the section value byte itself happens to be 0x02.

    Section byte: 0x00 = section 1, 0x01 = section 2, 0x02 = section 3.

    Args:
        data: Full decrypted config.
        name_offset: Offset of the 0x06 name tag.

    Returns:
        Section number (1-based). Defaults to 1 if not found.

    """
    # Search backwards up to 50 bytes for the section tag pattern.
    search_start = max(0, name_offset - 50)
    for j in range(name_offset - 4, search_start, -1):
        if (
            data[j] == _SECTION_TAG
            and j + 4 < len(data)
            and data[j + 2] == _TYPE_TAG
            and data[j + 4] == _EXTRA_TAG
            and data[j + 1] <= _MAX_SECTION_BYTE
        ):
            return data[j + 1] + 1

    return 1


def _find_rf_byte0(data: bytes, name_offset: int) -> int:
    """
    Find the RF address byte0 preceding a device name.

    The RF address is encoded as ``88 00 ce [rf_byte0] [b1] [b2]``
    in the record before the section/type/name tags. The byte0
    determines whether the device is wired/bus (<= 0x1f) or
    wireless (>= 0x20).

    Args:
        data: Full decrypted config.
        name_offset: Offset of the 0x06 name tag.

    Returns:
        RF byte0 value. Defaults to 0x00 if not found.

    """
    search_start = max(0, name_offset - 60)
    for j in range(name_offset - 4, search_start, -1):
        if data[j : j + 3] == _RF_MARKER and j + 3 < len(data):
            return data[j + 3]

    return 0x00


def _find_usb_device(hidraw_device_sysfs: Path) -> Path | None:
    """
    Walk up sysfs from a hidraw device entry to find the USB device.

    The path is: hidraw -> HID device -> USB interface -> USB device.
    We look for a directory containing ``idVendor`` and ``idProduct``
    files matching our panel.
    """
    current = hidraw_device_sysfs.resolve()
    for _ in range(6):
        id_vendor = current / "idVendor"
        id_product = current / "idProduct"
        if id_vendor.is_file() and id_product.is_file():
            try:
                vid = int(id_vendor.read_text().strip(), 16)
                pid = int(id_product.read_text().strip(), 16)
            except OSError, ValueError:
                pass
            else:
                if vid == USB_VENDOR_ID and pid == USB_PRODUCT_ID:
                    return current

        parent = current.parent
        if parent == current:
            break

        current = parent

    return None


def _find_usb_device_from_block(block_sysfs_entry: Path) -> Path | None:
    """
    Walk up sysfs from a block device entry to find the USB device.

    The path is: block -> SCSI disk -> SCSI host -> USB interface -> USB device.
    """
    device_link = block_sysfs_entry / "device"
    if not device_link.exists():
        return None

    current = device_link.resolve()
    for _ in range(8):
        id_vendor = current / "idVendor"
        id_product = current / "idProduct"
        if id_vendor.is_file() and id_product.is_file():
            try:
                vid = int(id_vendor.read_text().strip(), 16)
                pid = int(id_product.read_text().strip(), 16)
            except OSError, ValueError:
                pass
            else:
                if vid == USB_VENDOR_ID and pid == USB_PRODUCT_ID:
                    return current

        parent = current.parent
        if parent == current:
            break

        current = parent

    return None
