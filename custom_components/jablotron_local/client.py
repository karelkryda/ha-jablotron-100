"""
Blocking USB HID client for the Jablotron JA-100+ panel.

Runs a background reader thread that maintains the unauthenticated
monitoring connection. The reader sends only code-free packets
(heartbeat, enable-device-states) and dispatches parsed inbound data
to the coordinator via a callback.

This module performs raw ``/dev/hidraw*`` I/O using ``os.open``,
``os.read``, and ``os.write`` - no native dependencies beyond the
Linux kernel's HID subsystem. The panel expects 64-byte reports with
a leading zero report-ID byte prepended on write.

Thread safety
-------------
- The reader thread is the only writer during monitoring (heartbeat,
  enable-dev-states). A :class:`threading.Lock` serializes all writes
  so the command path can safely interleave.
- The callback is invoked from the reader thread. The coordinator is
  expected to use ``hass.loop.call_soon_threadsafe`` to bridge into
  the HA event loop.
- Command responses (LOGIN_INFO, COMMAND_ACK, WRONG_CODE) are detected
  by the reader thread and signalled to the command thread via
  :class:`threading.Event`.

Reconnect
---------
On any ``OSError`` during read or write, the client closes the file
descriptor and retries with exponential backoff (1 s, 2 s, 4 s, ...
capped at 60 s). On successful reconnect it re-sends the startup
sequence (sysinfo queries, get-sections, enable-device-states).
"""

import contextlib
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .const import LOGGER
from .protocol import (
    COMMAND_ACK_TIMEOUT,
    ENABLE_DEV_STATES_INTERVAL,
    HEARTBEAT_INTERVAL,
    LOGIN_TIMEOUT,
    REPORT_SIZE,
    ArmMode,
    Packet,
    PacketType,
    SysInfoType,
    UiControl,
    UiStatusReason,
    cmd_enable_device_states,
    cmd_get_sections_and_pg,
    cmd_get_system_info,
    cmd_heartbeat,
    decode_report,
    encode_report,
    ui_authorisation_code,
    ui_authorisation_end,
    ui_export_config,
    ui_modify_section,
)

# Report-ID byte prepended to every write for Linux hidraw.
_REPORT_ID = b"\x00"

# Reconnect backoff parameters.
_BACKOFF_INITIAL: float = 1.0
_BACKOFF_MAX: float = 60.0
_BACKOFF_FACTOR: float = 2.0

# Read timeout: poll interval for the select-less blocking read.
# We use O_NONBLOCK + sleep rather than a true poll() to keep the
# reconnect/shutdown logic simple and avoid platform quirks.
_READ_POLL_INTERVAL: float = 0.2

# Minimum payload length for a STATUS packet (subtype + reason byte).
_MIN_STATUS_BYTES: int = 2


type PacketCallback = Callable[[list[Packet]], None]
type ConnectionCallback = Callable[[bool], None]


class JablotronConnectionError(Exception):
    """Raised when the initial connection to the panel fails."""

    def __init__(self, path: str) -> None:
        """Store the device path."""
        self.path = path
        super().__init__(f"Cannot open Jablotron panel at {path}")


class JablotronAuthError(Exception):
    """Raised when the panel rejects the provided PIN."""

    def __init__(self) -> None:
        """Build a fixed message."""
        super().__init__("Panel rejected the PIN (wrong code)")


class JablotronCommandError(Exception):
    """Raised when a command fails (timeout or unexpected response)."""

    def __init__(self, detail: str) -> None:
        """Store the detail."""
        self.detail = detail
        super().__init__(f"Command failed: {detail}")


@dataclass
class JablotronClient:
    """
    Blocking USB HID client for unauthenticated panel monitoring.

    Lifecycle:

    1. Instantiate with the device path.
    2. Call :meth:`connect` from an executor thread. This opens the
       device and starts the reader thread.
    3. The reader thread runs until :meth:`disconnect` is called.
    4. Call :meth:`disconnect` from an executor thread on HA unload.

    Attributes:
        path: Absolute path to the hidraw character device.
        on_packets: Called from the reader thread with decoded packets
            for every inbound HID report.
        on_connection_change: Called from the reader thread when the
            connection state changes (``True`` = connected,
            ``False`` = disconnected / reconnecting).

    """

    path: str
    on_packets: PacketCallback | None = None
    on_connection_change: ConnectionCallback | None = None

    _fd: int = field(default=-1, init=False, repr=False)
    _write_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )
    _reader_thread: threading.Thread | None = field(
        default=None, init=False, repr=False
    )
    _stop_event: threading.Event = field(
        default_factory=threading.Event, init=False, repr=False
    )
    _connected: bool = field(default=False, init=False, repr=False)

    # Command response signalling: the reader thread sets these when it
    # sees LOGIN_INFO, WRONG_CODE, or COMMAND_ACK during a command.
    _cmd_login_event: threading.Event = field(
        default_factory=threading.Event, init=False, repr=False
    )
    _cmd_ack_event: threading.Event = field(
        default_factory=threading.Event, init=False, repr=False
    )
    _cmd_auth_error: bool = field(default=False, init=False, repr=False)
    command_in_progress: bool = field(default=False, init=False, repr=False)
    _cmd_export_done_event: threading.Event = field(
        default_factory=threading.Event, init=False, repr=False
    )

    def connect(self) -> None:
        """
        Open the device and start the reader thread.

        Blocking; call via ``hass.async_add_executor_job``.

        Raises:
            JablotronConnectionError: If the device cannot be opened.

        """
        self._open()
        self._stop_event.clear()
        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            name="jablotron-reader",
            daemon=True,
        )
        self._reader_thread.start()

    def disconnect(self) -> None:
        """
        Stop the reader thread and close the device.

        Blocking; call via ``hass.async_add_executor_job``.
        Safe to call multiple times.
        """
        self._stop_event.set()
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=5.0)
            self._reader_thread = None

        self._close()

    @property
    def connected(self) -> bool:
        """Return whether the client currently has an open device handle."""
        return self._connected

    # ------------------------------------------------------------------
    # Private: device I/O
    # ------------------------------------------------------------------

    def _open(self) -> None:
        """Open the hidraw device for read/write."""
        try:
            self._fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK | os.O_NOCTTY)
        except OSError as err:
            raise JablotronConnectionError(self.path) from err

        self._set_connected(connected=True)
        LOGGER.debug("Opened %s (fd=%d)", self.path, self._fd)

    def _close(self) -> None:
        """Close the device file descriptor if open."""
        if self._fd >= 0:
            with contextlib.suppress(OSError):
                os.close(self._fd)

            LOGGER.debug("Closed %s (fd=%d)", self.path, self._fd)
            self._fd = -1

        self._set_connected(connected=False)

    def _write_report(self, report: bytes) -> None:
        """
        Write a 64-byte HID report to the device.

        Prepends the report-ID byte (0x00) as required by Linux hidraw.
        Thread-safe via :attr:`_write_lock`.

        Raises:
            OSError: On write failure (triggers reconnect in reader loop).

        """
        with self._write_lock:
            os.write(self._fd, _REPORT_ID + report)

    def _read_report(self) -> bytes | None:
        """
        Attempt a non-blocking read of one 64-byte HID report.

        Returns:
            64 bytes on success, ``None`` if no data available.

        Raises:
            OSError: On read failure (triggers reconnect in reader loop).

        """
        try:
            data = os.read(self._fd, REPORT_SIZE)
        except BlockingIOError:
            return None

        if len(data) == REPORT_SIZE:
            return data

        if not data:
            msg = "Device returned empty read (disconnected)"
            raise OSError(msg)

        return None

    # ------------------------------------------------------------------
    # Private: reader thread
    # ------------------------------------------------------------------

    def _reader_loop(self) -> None:
        """Run the read/keepalive/reconnect loop until stopped."""
        self._send_startup()
        last_heartbeat = time.monotonic()
        last_enable = time.monotonic()

        while not self._stop_event.is_set():
            # --- Read ---
            try:
                report = self._read_report()
            except OSError:
                LOGGER.warning("Read error on %s; reconnecting", self.path)
                self._reconnect()
                last_heartbeat = time.monotonic()
                last_enable = time.monotonic()
                continue

            if report is not None:
                packets = decode_report(report)
                if packets and self.on_packets:
                    self.on_packets(packets)

                # Check for command responses and signal the command thread.
                self._check_command_responses(packets)

            # --- Keepalives ---
            now = time.monotonic()
            try:
                if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                    self._write_report(encode_report(cmd_heartbeat()))
                    last_heartbeat = now

                if now - last_enable >= ENABLE_DEV_STATES_INTERVAL:
                    self._write_report(encode_report(cmd_enable_device_states()))
                    last_enable = now
            except OSError:
                LOGGER.warning("Write error on %s; reconnecting", self.path)
                self._reconnect()
                last_heartbeat = time.monotonic()
                last_enable = time.monotonic()
                continue

            # Sleep briefly to avoid busy-spin on non-blocking reads.
            if report is None:
                self._stop_event.wait(timeout=_READ_POLL_INTERVAL)

    def _send_startup(self) -> None:
        """Send the initial query burst after connect/reconnect."""
        try:
            # System info queries (all known types).
            sysinfo_packets = [cmd_get_system_info(kind) for kind in SysInfoType]
            self._write_report(encode_report(*sysinfo_packets))

            # Request current sections + enable device state pushes.
            self._write_report(
                encode_report(cmd_get_sections_and_pg(), cmd_enable_device_states())
            )
        except OSError:
            LOGGER.debug("Startup write failed on %s", self.path)

    def _reconnect(self) -> None:
        """Close and re-open the device with exponential backoff."""
        self._close()
        backoff = _BACKOFF_INITIAL

        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=backoff)
            if self._stop_event.is_set():
                return

            try:
                self._fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK | os.O_NOCTTY)
            except OSError:
                backoff = min(backoff * _BACKOFF_FACTOR, _BACKOFF_MAX)
                LOGGER.debug(
                    "Reconnect to %s failed; next attempt in %.1fs",
                    self.path,
                    backoff,
                )
                continue

            LOGGER.info("Reconnected to %s", self.path)
            self._set_connected(connected=True)
            self._send_startup()
            return

    def _set_connected(self, *, connected: bool) -> None:
        """Update connection state and notify listener."""
        if self._connected == connected:
            return

        self._connected = connected
        if self.on_connection_change:
            self.on_connection_change(connected)

    # ------------------------------------------------------------------
    # Public: authenticated command path (called from executor)
    # ------------------------------------------------------------------

    def modify_section(self, section: int, mode: ArmMode, code: str) -> None:
        """
        Arm or disarm a section with the user's PIN.

        Executes the full authenticated command sequence:

        1. AUTH_END (clear any stale session)
        2. AUTH_CODE (prefix + code)
        3. Wait for LOGIN_INFO (0x0c) or WRONG_CODE (0x1b 0x03)
        4. MODIFY_SECTION
        5. Wait for COMMAND_ACK (0x1a)
        6. AUTH_END (logout)

        The PIN is held in memory only for the duration of this call
        (~20 ms on the wire). This method is blocking and MUST be
        called via ``hass.async_add_executor_job``.

        Args:
            section: Section number (1-based).
            mode: The desired :class:`ArmMode`.
            code: Full code string: 3-digit user prefix + PIN
                (e.g. ``"9991234"``). The caller prepends the prefix.

        Raises:
            JablotronAuthError: Panel rejected the PIN.
            JablotronCommandError: Timeout or unexpected response.
            JablotronConnectionError: Device not connected.

        """
        if not self._connected:
            raise JablotronConnectionError(self.path)

        prefix = code[:3]
        pin = code[3:]

        try:
            # 1. Clear stale session.
            self.command_in_progress = True
            self._write_report(encode_report(ui_authorisation_end()))

            # 2. Send auth code.
            self._write_report(encode_report(ui_authorisation_code(prefix, pin)))

            # 3. Wait for login response.
            self._wait_for_login_response()

            # 4. Send modify section command.
            self._write_report(encode_report(ui_modify_section(section, mode)))

            # 5. Wait for command ACK.
            self._wait_for_command_ack()
        finally:
            # 6. Always logout, even on error.
            with contextlib.suppress(OSError):
                self._write_report(encode_report(ui_authorisation_end()))

            self.command_in_progress = False

    def export_config(self, code: str) -> None:
        """
        Trigger config export to the FLEXI_CFG mass storage volume.

        Executes the authenticated export sequence:

        1. AUTH_END (clear any stale session)
        2. AUTH_CODE (service/installer PIN)
        3. Wait for LOGIN_INFO
        4. EXPORT_CONFIG (0x0f)
        5. Wait for COMMAND_ACK (0x1a)
        6. Wait for EXPORT_DONE (0x12) (~900ms)

        After this returns successfully, the config data is available
        at LBA 35-1955 on the FLEXI_CFG block device. The caller MUST
        read the data and then call :meth:`end_session` to logout.

        Args:
            code: Full code string: 3-digit user prefix + service PIN
                (e.g. ``"9991234"``).

        Raises:
            JablotronAuthError: Panel rejected the PIN.
            JablotronCommandError: Timeout or unexpected response.
            JablotronConnectionError: Device not connected.

        """
        if not self._connected:
            raise JablotronConnectionError(self.path)

        prefix = code[:3]
        pin = code[3:]

        try:
            # 1. Clear stale session.
            self.command_in_progress = True
            self._write_report(encode_report(ui_authorisation_end()))

            # 2. Send auth code.
            self._write_report(encode_report(ui_authorisation_code(prefix, pin)))

            # 3. Wait for login response.
            self._wait_for_login_response()

            # 4. Trigger config export.
            self._write_report(encode_report(ui_export_config()))

            # 5. Wait for ACK.
            self._wait_for_command_ack()

            # 6. Wait for export done (panel writes config to FLEXI_CFG).
            self._wait_for_export_done()
        except:
            # On error, logout immediately.
            with contextlib.suppress(OSError):
                self._write_report(encode_report(ui_authorisation_end()))

            self.command_in_progress = False
            raise

    def end_session(self) -> None:
        """
        End the authenticated session (logout).

        Must be called after :meth:`export_config` once the block
        device data has been read.
        """
        try:
            self._write_report(encode_report(ui_authorisation_end()))
        except OSError:
            LOGGER.debug("Logout write failed")
        finally:
            self.command_in_progress = False

    def _wait_for_export_done(self) -> None:
        """
        Wait for the reader thread to signal EXPORT_DONE (0x12).

        The panel takes ~900ms to write config to FLEXI_CFG.

        Raises:
            JablotronCommandError: On timeout.

        """
        self._cmd_export_done_event.clear()

        if not self._cmd_export_done_event.wait(timeout=5.0):
            msg = "Config export timeout (no 0x12 received)"
            raise JablotronCommandError(msg)

    def _wait_for_login_response(self) -> None:
        """
        Wait for the reader thread to signal login success or failure.

        Raises:
            JablotronAuthError: On WRONG_CODE.
            JablotronCommandError: On timeout.

        """
        self._cmd_login_event.clear()
        self._cmd_auth_error = False

        if not self._cmd_login_event.wait(timeout=LOGIN_TIMEOUT):
            msg = "Login response timeout"
            raise JablotronCommandError(msg)

        if self._cmd_auth_error:
            raise JablotronAuthError

    def _wait_for_command_ack(self) -> None:
        """
        Wait for the reader thread to signal COMMAND_ACK.

        Raises:
            JablotronCommandError: On timeout.

        """
        self._cmd_ack_event.clear()

        if not self._cmd_ack_event.wait(timeout=COMMAND_ACK_TIMEOUT):
            msg = "Command ACK timeout"
            raise JablotronCommandError(msg)

    def _check_command_responses(self, packets: list[Packet]) -> None:
        """Check decoded packets for command-response subtypes and signal."""
        for packet in packets:
            if packet.type != PacketType.UI_CONTROL or not packet.data:
                continue

            subtype = packet.data[0]
            if subtype == UiControl.LOGIN_INFO:
                LOGGER.debug("Login confirmed by panel")
                self._cmd_login_event.set()
            elif subtype == UiControl.COMMAND_ACK:
                LOGGER.debug("Command ACK received")
                self._cmd_ack_event.set()
            elif subtype == UiControl.EXPORT_DONE:
                LOGGER.debug("Config export done")
                self._cmd_export_done_event.set()
            elif (
                subtype == UiControl.STATUS
                and len(packet.data) >= _MIN_STATUS_BYTES
                and packet.data[1] == UiStatusReason.WRONG_CODE
            ):
                LOGGER.debug("Wrong code reported by panel")
                self._cmd_auth_error = True
                self._cmd_login_event.set()
