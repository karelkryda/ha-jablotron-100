"""
HID device infrastructure for the Jablotron Local integration.

Low-level helpers for enumerating and opening Jablotron JA-100+ panels
exposed as ``/dev/hidraw*`` character devices on Linux. This module
performs no protocol-level operations - it only discovers devices and
verifies that the host can obtain exclusive read/write access.

The panel identifies as a raw HID device (USB VID/PID
``0x16D6``/``0x0008``) and is exposed by the kernel's ``hid-generic``
driver. JA-Link / F-Link holds the device exclusively when running, so
opening the character device is also the correct probe for "is any
other process using the panel right now".

All functions in this module are blocking and MUST be invoked via
``hass.async_add_executor_job``.
"""

import errno
import os
from dataclasses import dataclass
from pathlib import Path

from .const import LOGGER, USB_PRODUCT_ID, USB_VENDOR_ID

SYSFS_HIDRAW = Path("/sys/class/hidraw")
DEV_PATH = Path("/dev")

# HID_ID line format: BUS:VENDOR:PRODUCT (hex, zero-padded)
_HID_ID_PARTS = 3


@dataclass(frozen=True, slots=True)
class DiscoveredPanel:
    """
    A Jablotron panel discovered on the host.

    Attributes:
        path: The character device path (e.g. ``/dev/hidraw3``).
        serial: The unique serial number from ``HID_UNIQ``. Empty
            string if the panel does not report a serial.
        name: Human-readable device name from ``HID_NAME``, used only
            for UI display. Not stable across kernel versions.

    """

    path: str
    serial: str
    name: str


class HidrawError(Exception):
    """
    Base class for hidraw infrastructure errors.

    Concrete subclasses format their own message from the device path
    to keep call sites free of message-construction noise.
    """

    _template: str = "hidraw error at {path}"

    def __init__(self, path: str) -> None:
        """Store the offending device path and build a formatted message."""
        self.path = path
        super().__init__(self._template.format(path=path))


class DeviceNotFoundError(HidrawError):
    """The configured hidraw path no longer exists."""

    _template = "Device path does not exist: {path}"


class PermissionDeniedError(HidrawError):
    """The Home Assistant process lacks read/write access to the device."""

    _template = "Permission denied opening {path}"


class DeviceBusyError(HidrawError):
    """Another process (typically JA-Link / F-Link) holds the device."""

    _template = "Device is held by another process: {path}"


class DeviceOpenError(HidrawError):
    """Any other OS-level failure opening the device."""

    _template = "Failed to open {path}"


def enumerate_panels() -> list[DiscoveredPanel]:
    """
    Enumerate Jablotron panels currently connected to the host.

    Scans ``/sys/class/hidraw/`` for devices matching the Jablotron
    JA-100+ VID/PID and returns them as typed records. Returns an
    empty list on non-Linux hosts where sysfs is unavailable.

    Blocking; call via ``async_add_executor_job``.

    Returns:
        List of discovered panels. Order is stable per kernel
        enumeration order.

    """
    if not SYSFS_HIDRAW.is_dir():
        LOGGER.debug("sysfs hidraw directory not present; enumeration skipped")
        return []

    panels: list[DiscoveredPanel] = []
    for entry in sorted(SYSFS_HIDRAW.iterdir()):
        uevent_path = entry / "device" / "uevent"
        if not uevent_path.is_file():
            continue

        try:
            uevent = uevent_path.read_text(encoding="utf-8")
        except OSError as err:
            LOGGER.debug("Failed to read %s: %s", uevent_path, err)
            continue

        vid, pid, name, serial = _parse_uevent(uevent)
        if vid != USB_VENDOR_ID or pid != USB_PRODUCT_ID:
            continue

        raw_path = str(DEV_PATH / entry.name)
        panels.append(
            DiscoveredPanel(
                path=_find_stable_symlink(raw_path),
                serial=serial,
                name=name or "Jablotron Panel",
            )
        )

    return panels


def _find_stable_symlink(hidraw_path: str) -> str:
    """
    Find a stable /dev symlink pointing to the given hidraw device.

    Scans /dev/ for a symlink whose target resolves to the same device.
    If none exist, returns the original path unchanged.

    Args:
        hidraw_path: Raw device path, e.g. ``/dev/hidraw1``.

    Returns:
        A stable symlink path, or the original path if no symlink found.

    """
    try:
        real_target = Path(hidraw_path).resolve()
    except OSError:
        return hidraw_path

    try:
        for entry in DEV_PATH.iterdir():
            if not entry.is_symlink():
                continue

            try:
                if entry.resolve() == real_target:
                    return str(entry)
            except OSError:
                continue
    except OSError:
        return hidraw_path

    return hidraw_path


def probe_device(path: str) -> None:
    """
    Verify the host can open the panel with exclusive read/write access.

    Opens the character device with ``O_RDWR`` and immediately closes
    it. This is the minimum viable connectivity test - it validates:

    - the device path exists,
    - the HA process has read/write permission,
    - no other process (JA-Link, F-Link, another HA integration) is
      holding the device.

    Blocking; call via ``async_add_executor_job``.

    Args:
        path: Absolute path to the character device, e.g. ``/dev/hidraw0``.

    Raises:
        DeviceNotFoundError: The path does not exist.
        PermissionDeniedError: HA lacks read/write permission on the path.
        DeviceBusyError: Another process holds the device.
        DeviceOpenError: Any other OS-level failure opening the device.

    """
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
    except FileNotFoundError as err:
        raise DeviceNotFoundError(path) from err
    except PermissionError as err:
        raise PermissionDeniedError(path) from err
    except BlockingIOError as err:
        raise DeviceBusyError(path) from err
    except OSError as err:
        if err.errno == errno.EBUSY:
            raise DeviceBusyError(path) from err

        raise DeviceOpenError(path) from err
    else:
        os.close(fd)


def _parse_uevent(content: str) -> tuple[int, int, str, str]:
    """
    Parse a hidraw device ``uevent`` file.

    The kernel exposes lines of the form ``KEY=VALUE``. We only need
    ``HID_ID`` (bus/VID/PID), ``HID_NAME`` and ``HID_UNIQ`` (serial).

    Args:
        content: Full ``uevent`` file contents.

    Returns:
        A tuple of ``(vendor_id, product_id, name, serial)``. Missing
        fields are returned as zero / empty string.

    """
    vid = 0
    pid = 0
    name = ""
    serial = ""

    for line in content.splitlines():
        if line.startswith("HID_ID="):
            parts = line.removeprefix("HID_ID=").split(":")
            if len(parts) >= _HID_ID_PARTS:
                try:
                    vid = int(parts[1], 16)
                    pid = int(parts[2], 16)
                except ValueError:
                    LOGGER.debug("Malformed HID_ID line: %s", line)
        elif line.startswith("HID_NAME="):
            name = line.removeprefix("HID_NAME=")
        elif line.startswith("HID_UNIQ="):
            serial = line.removeprefix("HID_UNIQ=")

    return vid, pid, name, serial
