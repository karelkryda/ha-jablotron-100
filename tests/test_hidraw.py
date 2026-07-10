"""Tests for custom_components.jablotron_local.hidraw."""

import errno
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from custom_components.jablotron_local.hidraw import (
    DeviceBusyError,
    DeviceNotFoundError,
    DeviceOpenError,
    DiscoveredPanel,
    PermissionDeniedError,
    _find_stable_symlink,
    enumerate_panels,
    probe_device,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_JABLOTRON_UEVENT = """\
HID_ID=0003:000016D6:00000008
HID_NAME=JABLOTRON JA-100
HID_UNIQ=JA103K-0000001
"""

_OTHER_DEVICE_UEVENT = """\
HID_ID=0003:00001234:00005678
HID_NAME=Some Other Device
HID_UNIQ=
"""

_MALFORMED_UEVENT = """\
HID_ID=0003:ZZZZ:XXXX
HID_NAME=BadDevice
"""


# ---------------------------------------------------------------------------
# enumerate_panels
# ---------------------------------------------------------------------------


class TestEnumeratePanels:
    def test_returns_empty_when_sysfs_missing(self, tmp_path: Path):
        nonexistent = tmp_path / "nonexistent"
        with patch(
            "custom_components.jablotron_local.hidraw.SYSFS_HIDRAW",
            nonexistent,
        ):
            assert enumerate_panels() == []

    def test_finds_jablotron_panel(self, tmp_path: Path):
        hidraw_dir = tmp_path / "hidraw0" / "device"
        hidraw_dir.mkdir(parents=True)
        (hidraw_dir / "uevent").write_text(_JABLOTRON_UEVENT)

        with patch(
            "custom_components.jablotron_local.hidraw.SYSFS_HIDRAW",
            tmp_path,
        ):
            panels = enumerate_panels()

        assert len(panels) == 1
        assert panels[0] == DiscoveredPanel(
            path="/dev/hidraw0",
            serial="JA103K-0000001",
            name="JABLOTRON JA-100",
        )

    def test_skips_non_jablotron_device(self, tmp_path: Path):
        hidraw_dir = tmp_path / "hidraw0" / "device"
        hidraw_dir.mkdir(parents=True)
        (hidraw_dir / "uevent").write_text(_OTHER_DEVICE_UEVENT)

        with patch(
            "custom_components.jablotron_local.hidraw.SYSFS_HIDRAW",
            tmp_path,
        ):
            assert enumerate_panels() == []

    def test_skips_malformed_hid_id(self, tmp_path: Path):
        hidraw_dir = tmp_path / "hidraw0" / "device"
        hidraw_dir.mkdir(parents=True)
        (hidraw_dir / "uevent").write_text(_MALFORMED_UEVENT)

        with patch(
            "custom_components.jablotron_local.hidraw.SYSFS_HIDRAW",
            tmp_path,
        ):
            assert enumerate_panels() == []

    def test_multiple_panels_sorted(self, tmp_path: Path):
        for name in ("hidraw2", "hidraw0", "hidraw1"):
            device_dir = tmp_path / name / "device"
            device_dir.mkdir(parents=True)
            (device_dir / "uevent").write_text(_JABLOTRON_UEVENT)

        with patch(
            "custom_components.jablotron_local.hidraw.SYSFS_HIDRAW",
            tmp_path,
        ):
            panels = enumerate_panels()

        assert len(panels) == 3
        assert [p.path for p in panels] == [
            "/dev/hidraw0",
            "/dev/hidraw1",
            "/dev/hidraw2",
        ]

    def test_skips_entry_without_uevent(self, tmp_path: Path):
        (tmp_path / "hidraw0" / "device").mkdir(parents=True)

        with patch(
            "custom_components.jablotron_local.hidraw.SYSFS_HIDRAW",
            tmp_path,
        ):
            assert enumerate_panels() == []

    def test_handles_unreadable_uevent(self, tmp_path: Path):
        hidraw_dir = tmp_path / "hidraw0" / "device"
        hidraw_dir.mkdir(parents=True)
        uevent_path = hidraw_dir / "uevent"
        uevent_path.write_text(_JABLOTRON_UEVENT)

        with (
            patch(
                "custom_components.jablotron_local.hidraw.SYSFS_HIDRAW",
                tmp_path,
            ),
            patch.object(
                Path,
                "read_text",
                side_effect=OSError("Permission denied"),
            ),
        ):
            assert enumerate_panels() == []

    def test_uses_default_name_when_hid_name_missing(self, tmp_path: Path):
        uevent = "HID_ID=0003:000016D6:00000008\nHID_UNIQ=SN123\n"
        hidraw_dir = tmp_path / "hidraw0" / "device"
        hidraw_dir.mkdir(parents=True)
        (hidraw_dir / "uevent").write_text(uevent)

        with patch(
            "custom_components.jablotron_local.hidraw.SYSFS_HIDRAW",
            tmp_path,
        ):
            panels = enumerate_panels()

        assert panels[0].name == "Jablotron Panel"

    def test_empty_serial_when_hid_uniq_missing(self, tmp_path: Path):
        uevent = "HID_ID=0003:000016D6:00000008\nHID_NAME=Panel\n"
        hidraw_dir = tmp_path / "hidraw0" / "device"
        hidraw_dir.mkdir(parents=True)
        (hidraw_dir / "uevent").write_text(uevent)

        with patch(
            "custom_components.jablotron_local.hidraw.SYSFS_HIDRAW",
            tmp_path,
        ):
            panels = enumerate_panels()

        assert panels[0].serial == ""


# ---------------------------------------------------------------------------
# probe_device
# ---------------------------------------------------------------------------


class TestProbeDevice:
    def test_success(self):
        mock_fd = 42
        with (
            patch("os.open", return_value=mock_fd) as mock_open,
            patch("os.close") as mock_close,
        ):
            probe_device("/dev/hidraw3")

        mock_open.assert_called_once_with("/dev/hidraw3", os.O_RDWR | os.O_NOCTTY)
        mock_close.assert_called_once_with(mock_fd)

    def test_file_not_found_raises_device_not_found(self):
        with (
            patch("os.open", side_effect=FileNotFoundError),
            pytest.raises(DeviceNotFoundError) as exc_info,
        ):
            probe_device("/dev/hidraw99")

        assert exc_info.value.path == "/dev/hidraw99"

    def test_permission_error_raises_permission_denied(self):
        with (
            patch("os.open", side_effect=PermissionError),
            pytest.raises(PermissionDeniedError) as exc_info,
        ):
            probe_device("/dev/hidraw0")

        assert exc_info.value.path == "/dev/hidraw0"

    def test_blocking_io_error_raises_device_busy(self):
        with (
            patch("os.open", side_effect=BlockingIOError),
            pytest.raises(DeviceBusyError) as exc_info,
        ):
            probe_device("/dev/hidraw0")

        assert exc_info.value.path == "/dev/hidraw0"

    def test_ebusy_raises_device_busy(self):
        err = OSError(errno.EBUSY, "Device or resource busy")
        with (
            patch("os.open", side_effect=err),
            pytest.raises(DeviceBusyError) as exc_info,
        ):
            probe_device("/dev/hidraw0")

        assert exc_info.value.path == "/dev/hidraw0"

    def test_other_os_error_raises_device_open_error(self):
        err = OSError(errno.EIO, "I/O error")
        with (
            patch("os.open", side_effect=err),
            pytest.raises(DeviceOpenError) as exc_info,
        ):
            probe_device("/dev/hidraw0")

        assert exc_info.value.path == "/dev/hidraw0"


# ---------------------------------------------------------------------------
# _find_stable_symlink
# ---------------------------------------------------------------------------


class TestFindStableSymlink:
    def test_returns_symlink_when_exists(self, tmp_path: Path):
        # Create a fake hidraw device file.
        hidraw = tmp_path / "hidraw1"
        hidraw.touch()

        # Create a symlink pointing to it.
        symlink = tmp_path / "jablotron-hid"
        symlink.symlink_to("hidraw1")

        with patch(
            "custom_components.jablotron_local.hidraw.DEV_PATH",
            tmp_path,
        ):
            result = _find_stable_symlink(str(hidraw))

        assert result == str(symlink)

    def test_returns_original_when_no_symlink(self, tmp_path: Path):
        # tmp_path has no symlinks.
        hidraw = tmp_path / "hidraw1"
        hidraw.touch()

        with patch(
            "custom_components.jablotron_local.hidraw.DEV_PATH",
            tmp_path,
        ):
            result = _find_stable_symlink(str(hidraw))

        assert result == str(hidraw)

    def test_returns_original_when_dev_path_unreadable(self, tmp_path: Path):
        nonexistent = tmp_path / "nonexistent"
        with patch(
            "custom_components.jablotron_local.hidraw.DEV_PATH",
            nonexistent,
        ):
            result = _find_stable_symlink("/dev/hidraw0")

        assert result == "/dev/hidraw0"

    def test_returns_original_when_resolve_fails(self):
        with patch(
            "pathlib.Path.resolve",
            side_effect=OSError("nope"),
        ):
            result = _find_stable_symlink("/dev/hidraw0")

        assert result == "/dev/hidraw0"

    def test_skips_non_symlink_entries(self, tmp_path: Path):
        # Regular file with a tempting name - should be ignored.
        hidraw = tmp_path / "hidraw1"
        hidraw.touch()
        (tmp_path / "jablotron-hid").touch()  # regular file, not symlink

        with patch(
            "custom_components.jablotron_local.hidraw.DEV_PATH",
            tmp_path,
        ):
            result = _find_stable_symlink(str(hidraw))

        assert result == str(hidraw)

    def test_skips_symlinks_to_other_targets(self, tmp_path: Path):
        hidraw1 = tmp_path / "hidraw1"
        hidraw1.touch()
        hidraw2 = tmp_path / "hidraw2"
        hidraw2.touch()

        # Symlink points to hidraw2, not hidraw1.
        symlink = tmp_path / "jablotron-hid"
        symlink.symlink_to("hidraw2")

        with patch(
            "custom_components.jablotron_local.hidraw.DEV_PATH",
            tmp_path,
        ):
            result = _find_stable_symlink(str(hidraw1))

        assert result == str(hidraw1)


# ---------------------------------------------------------------------------
# enumerate_panels with symlink
# ---------------------------------------------------------------------------


class TestEnumeratePanelsSymlink:
    def test_uses_symlink_when_available(self, tmp_path: Path):
        # Set up sysfs.
        sysfs = tmp_path / "sysfs"
        hidraw_dir = sysfs / "hidraw1" / "device"
        hidraw_dir.mkdir(parents=True)
        (hidraw_dir / "uevent").write_text(_JABLOTRON_UEVENT)

        # Set up /dev with symlink.
        dev = tmp_path / "dev"
        dev.mkdir()
        (dev / "hidraw1").touch()
        (dev / "jablotron-hid").symlink_to("hidraw1")

        with (
            patch(
                "custom_components.jablotron_local.hidraw.SYSFS_HIDRAW",
                sysfs,
            ),
            patch(
                "custom_components.jablotron_local.hidraw.DEV_PATH",
                dev,
            ),
        ):
            panels = enumerate_panels()

        assert len(panels) == 1
        assert panels[0].path == str(dev / "jablotron-hid")
