r"""
TLV framing codec for the Jablotron JA-100+ USB HID protocol.

Transport
---------
The panel communicates over USB HID. The host reads 64-byte interrupt
reports from endpoint 0x81 (IN) and sends 64-byte reports via a
SET_REPORT control transfer (OUT). Every report carries one or more
concatenated TLV packets, zero-padded to exactly 64 bytes::

    TYPE(1) | LEN(1) | DATA(LEN) | TYPE(1) | LEN(1) | DATA(LEN) | ...
    <-------- 64 bytes, zero-padded --------------------------------->

Directions
----------
All operation in this integration falls into exactly two modes:

MONITORING (unauthenticated, permanent)
    The reader thread sends only code-free packets:

    - heartbeat (52 01 02)
    - enable-dev-states (52 02 13 05)

    The panel pushes section states, device events and device-activity
    bitmaps. No PIN is ever involved.

COMMAND (authenticated, short-lived sessions)
    Arm/disarm sequence verified against USB capture from JA-Link:

        80 01 01          AUTH_END  (clear stale session)
        80 <n> 03 <ascii> AUTH_CODE (prefix + pin, ASCII)
        ...wait for 80 0c (ok) or 80 1b 03 (wrong code)...
        80 02 0d <byte>   MODIFY_SECTION
        ...wait for 80 1a (ACK)...
        80 01 01          AUTH_END  (logout)

    Device status query (requires auth):

        52 02 28 <dev>    QUERY_DEVICE_STATUS
        ...response: 52 xx a8 <dev> [signal/battery data]...

    Bus device diagnostics (requires auth):

        94 02 <dev> 01    DIAGNOSTICS_START
        96 03 <dev> 09 00 DIAGNOSTICS_FORCE_INFO
        ...response: 90 [len] <dev> 0a [signal/battery/voltage]...
        94 02 <dev> 00    DIAGNOSTICS_STOP

Code encoding (pcap-verified)
------------------------------
Wire format is: subtype 0x03 followed by ``(prefix + pin).encode('ascii')``.
For example, user 999, PIN 1234 gives ``03 39 39 39 31 32 33 34``.
This is simply ``(prefix + pin).encode('ascii')`` since ``chr(48 + d) == str(d)``
for d in 0..9.

MODIFY_SECTION byte (pcap-verified for section 2)
--------------------------------------------------
- Section 2 arm:    0x9f + 2 = 0xa1 (verified by capture)
- Section 2 disarm: 0x8f + 2 = 0x91 (verified by capture)

Other sections and ARM_HOME/ARM_NIGHT use the same formula but are
not yet confirmed by capture; they need a service-window test.

Timing constants (observed from captures)
-------------------------------------------
- Heartbeat cadence:          1.0 s
- Enable-dev-states refresh:  60.0 s
- Login ACK latency:          ~7 ms (timeout 2 s)
- MODIFY_SECTION ACK latency: ~13 ms (timeout 2 s)
- State confirm latency:      ~40 ms (timeout 5 s)
"""

from dataclasses import dataclass
from enum import IntEnum

REPORT_SIZE: int = 64
"""Size of every HID report in bytes."""

CODE_MIN_LENGTH: int = 4
"""Minimum PIN length accepted by the panel."""

CODE_MAX_LENGTH: int = 10
"""Maximum PIN length accepted by the panel."""

CODE_PREFIX_WILDCARD: str = "999"
"""Wildcard user-index prefix for code encoding.

Tells the panel to match the PIN against all configured users.
Used on panels with CodesWithPrefix=false.
"""

# ---------------------------------------------------------------------------
# Timing (seconds) - use these in client.py, not magic numbers
# ---------------------------------------------------------------------------

HEARTBEAT_INTERVAL: float = 1.0
ENABLE_DEV_STATES_INTERVAL: float = 60.0
ENABLE_DEV_STATES_MINUTES: int = 5
LOGIN_TIMEOUT: float = 2.0
COMMAND_ACK_TIMEOUT: float = 2.0
STATE_CONFIRM_TIMEOUT: float = 5.0
DEVICE_STATUS_TIMEOUT: float = 2.0


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class PacketType(IntEnum):
    """USB HID packet type byte (first byte of every TLV atom)."""

    GET_SYS_INFO = 0x30  # OUT: query a system-info field
    SYS_INFO = 0x40  # IN: system-info reply
    PG_OUTPUTS = 0x50  # IN: PG output states
    SECTIONS = 0x51  # IN: section states (pushed by panel)
    COMMAND = 0x52  # OUT: code-free commands (heartbeat, etc.)
    DEVICE_STATE = 0x55  # IN: individual device event
    UI_CONTROL = 0x80  # BOTH: authentication and section control
    DEVICE_INFO = 0x90  # IN: device details (response to 94/96 diag)
    DIAGNOSTICS = 0x94  # OUT: start/stop diagnostics for a bus device
    DIAGNOSTICS_COMMAND = 0x96  # OUT: force info report from bus device
    DEVICES_STATES = 0xD8  # IN: activity bitmap (little-endian)


class Command(IntEnum):
    """Subtype byte for :attr:`PacketType.COMMAND` packets (OUT)."""

    HEARTBEAT = 0x02  # 52 01 02 - link keepalive, unauthenticated
    GET_SECTIONS_AND_PG = 0x0E  # 52 01 0e - request current sections + PG
    ENABLE_DEV_STATES = 0x13  # 52 02 13 <minutes>
    QUERY_DEVICE_STATUS = 0x28  # 52 02 28 <n> - requires auth
    QUERY_DEVICE_STATUS_RESPONSE = 0xA8  # 52 xx a8 <n> ... - response to 0x28


class UiControl(IntEnum):
    """Subtype byte for :attr:`PacketType.UI_CONTROL` packets."""

    AUTHORISATION_END = 0x01  # OUT/IN: logout / clear session
    SESSION_KEEPALIVE = 0x02  # OUT (authenticated only) - never sent here
    AUTHORISATION_CODE = 0x03  # OUT: login (prefix + pin as ASCII)
    MODIFY_SECTION = 0x0D  # OUT: arm / disarm a section
    LOGIN_INFO = 0x0C  # IN: login confirmation (user permissions)
    COMMAND_ACK = 0x1A  # IN: command accepted
    STATUS = 0x1B  # IN: status / NAK
    TOGGLE_PG_OUTPUT = 0x23  # OUT: toggle a PG output
    EXPORT_CONFIG = 0x0F  # OUT: trigger config export to FLEXI_CFG
    EXPORT_DONE = 0x12  # IN: config export complete


class UiStatusReason(IntEnum):
    """Reason byte within a :attr:`UiControl.STATUS` packet (80 1b <reason>)."""

    WRONG_CODE = 0x03  # 80 1b 03 - wrong PIN; hard abort, never retry
    NO_SESSION = 0x06  # 80 1b 06 - no active session (benign for reader)


class SysInfoType(IntEnum):
    """Info-type byte for GET_SYS_INFO / SYS_INFO packets."""

    MODEL = 0x02
    # NOTE: 0x08 = FIRMWARE, 0x09 = HARDWARE.
    FIRMWARE = 0x08
    HARDWARE = 0x09
    REG_CODE = 0x0A
    NAME = 0x0B
    MAC = 0x0C


class SectionPrimaryState(IntEnum):
    """
    Primary state of a section.

    Taken from bits [2:0] of the first byte of each 2-byte slot in a
    :attr:`PacketType.SECTIONS` payload.
    """

    UNKNOWN = -1
    UNSET = 0
    DISARMED = 1
    ARMED_PARTIAL = 2
    ARMED_FULL = 3
    MAINTENANCE = 4
    SERVICE = 5
    BLOCKED = 6
    OFF = 7

    @classmethod
    def _missing_(cls, value: object) -> SectionPrimaryState:  # noqa: ARG003
        """Return UNKNOWN for any unrecognised value."""
        return cls.UNKNOWN


class SectionSecondaryState(IntEnum):
    """
    Secondary (transitional) state of a section.

    Derived from flag bits of the primary state byte in the
    :attr:`PacketType.SECTIONS` payload. When multiple flags are set,
    the highest-priority state is selected via :data:`_SECONDARY_FLAGS`.
    """

    UNKNOWN = -1
    NORMAL = 0
    PENDING = 1
    ARMING = 2
    TRIGGERED = 3

    @classmethod
    def _missing_(cls, value: object) -> SectionSecondaryState:  # noqa: ARG003
        """Return UNKNOWN for any unrecognised value."""
        return cls.UNKNOWN


class ArmMode(IntEnum):
    """
    Arm mode used as input to :func:`ui_modify_section`.

    The MODIFY_SECTION byte is computed as ``_MODIFY_BASE[mode] + section``.
    Section 2 arm (0xa1) and disarm (0x91) are pcap-verified.
    All other section numbers and ARM_HOME / ARM_NIGHT are formula-derived
    and need a service-window test to confirm.
    """

    DISARM = 0x8F  # base: section 2 disarm = 0x91 ✓ pcap
    ARM_AWAY = 0x9F  # base: section 2 arm    = 0xa1 ✓ pcap
    ARM_HOME = 0xAF  # base: formula-derived, not yet pcap-verified
    ARM_NIGHT = 0xAF  # base: same as ARM_HOME, not yet verified


# ---------------------------------------------------------------------------
# Dataclasses (parsed inbound data, all frozen / slots for thread safety)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Packet:
    """
    A single TLV atom extracted from a HID report.

    Attributes:
        type: The packet type byte.
        data: The DATA field (length LEN from the TLV header).

    """

    type: int
    data: bytes


@dataclass(frozen=True, slots=True)
class SectionState:
    """
    State of one alarm section.

    Decoded from a :attr:`PacketType.SECTIONS` report.

    Attributes:
        number: Section number, 1-based.
        primary: Primary state value (see :class:`SectionPrimaryState`).
        secondary: Transitional state (see :class:`SectionSecondaryState`).
        flags: Secondary flags byte (semantics partially unknown; 0x00
            in normal operation).

    """

    number: int
    primary: SectionPrimaryState
    secondary: SectionSecondaryState
    flags: int


@dataclass(frozen=True, slots=True)
class DeviceActivity:
    """
    Device activity snapshot from a :attr:`PacketType.DEVICES_STATES` report.

    Attributes:
        active: Frozenset of device numbers (1-based) currently active
            (triggered / reporting motion, open, etc.).

    """

    active: frozenset[int]


@dataclass(frozen=True, slots=True)
class SystemInfo:
    """
    One system-info field from a :attr:`PacketType.SYS_INFO` reply.

    Attributes:
        kind: The info-type byte (see :class:`SysInfoType`).
        value: Decoded string value. MAC addresses are formatted as
            colon-separated hex (e.g. ``"00:11:22:33:44:55"``).

    """

    kind: SysInfoType
    value: str


@dataclass(frozen=True, slots=True)
class UiStatusEvent:
    """
    A status / NAK from the panel (packet ``80 1b <reason> ...``).

    Attributes:
        reason: Reason byte (see :class:`UiStatusReason`).
        raw: Full data payload for logging.

    """

    reason: int
    raw: bytes


@dataclass(frozen=True, slots=True)
class DeviceStatus:
    """
    Status of a single device from a ``52 xx a8`` response.

    Attributes:
        device_number: 0-based device number.
        signal: Signal strength percentage (0-100), or None if not in response.
        battery: Battery level percentage (0-100), or None if not in response.
        active: Whether the device is currently active/triggered.

    """

    device_number: int
    signal: int | None
    battery: int | None
    active: bool


@dataclass(frozen=True, slots=True)
class DeviceDiagnostic:
    """
    Parsed diagnostics response from ``94``/``96`` sequence (0x90 reply).

    Attributes:
        device_number: 0-based device number.
        signal: Bus signal quality percentage (0-100).
        battery: Battery percentage (0-100), or None if bus-powered.
        voltage: Battery voltage in volts (sirens), or None.
        voltage_current: Current battery voltage (sirens), or None.

    """

    device_number: int
    signal: int
    battery: int | None
    voltage: float | None
    voltage_current: float | None


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """
    Consolidated device info used by sensor entities.

    Built from DeviceStatus (0x28 response) and optionally enriched
    by DeviceDiagnostic (94/96 response) for bus devices.

    Attributes:
        device_number: 0-based device number.
        signal: Signal strength percentage (0-100), or None.
        battery: Battery level percentage (0-100), or None.
        voltage: Battery voltage in volts (sirens), or None.
        voltage_current: Current battery voltage in volts (sirens), or None.
        active: Whether the device is currently active/triggered.

    """

    device_number: int
    signal: int | None
    battery: int | None
    voltage: float | None = None
    voltage_current: float | None = None
    active: bool = False


# Diagnostics subtype in 0x90 response indicating requested info.
_DEVICE_INFO_REQUESTED: int = 0x0A
# Minimum response length for bus device info (dev + subtype + len + flags + signal).
_MIN_BUS_INFO_BYTES: int = 5
# Offset of signal byte in bus device info response.
_BUS_INFO_SIGNAL_OFFSET: int = 4

# Wireless marker byte separating activity from radio data.
_WIRELESS_MARKER: int = 0xFC
# Wired marker byte.
_WIRED_MARKER: int = 0xF2

# Battery special values: bytes >= this threshold are not real levels
# (would yield >100% if decoded). Exact meanings unverified for 0xa8 responses.
_BATTERY_SPECIAL_THRESHOLD: int = 0x0B

# Battery level calculation.
_BATTERY_LEVEL_STEP: int = 10
_BATTERY_MAX_PERCENT: int = 100

# Minimum response length for device status (subtype + device_number + flags).
_MIN_DEVICE_STATUS_BYTES: int = 3
# Byte offset of the activity field in device status response.
_ACTIVITY_OFFSET: int = 4
# Activity field minimum data length for wired check.
_WIRED_ACTIVITY_END: int = 5
# Activity value meaning "never communicated".
_ACTIVITY_NEVER: int = 0xFF


# ---------------------------------------------------------------------------
# Report codec
# ---------------------------------------------------------------------------


class ReportTooLongError(ValueError):
    """Raised when encoded packets exceed :data:`REPORT_SIZE` bytes."""

    def __init__(self, length: int) -> None:
        """Store actual length and build a message."""
        self.length = length
        super().__init__(f"Encoded report is {length} bytes; maximum is {REPORT_SIZE}")


def encode_report(*packets: Packet) -> bytes:
    """
    Encode one or more TLV packets into a 64-byte HID report.

    Concatenates packets as ``TYPE LEN DATA ...`` and zero-pads to
    exactly :data:`REPORT_SIZE` bytes.

    Args:
        *packets: One or more :class:`Packet` instances to encode.

    Returns:
        Exactly 64 bytes, ready to pass to the HID ``write()`` call
        (prepend a zero report-ID byte for hidapi).

    Raises:
        ReportTooLongError: If the encoded packets exceed 64 bytes.

    """
    parts: list[bytes] = [
        bytes([pkt.type, len(pkt.data)]) + pkt.data for pkt in packets
    ]
    body = b"".join(parts)
    if len(body) > REPORT_SIZE:
        raise ReportTooLongError(len(body))

    return body.ljust(REPORT_SIZE, b"\x00")


def decode_report(report: bytes) -> list[Packet]:
    """
    Split a 64-byte HID report into its constituent TLV packets.

    Stops at the first zero type byte (padding). Silently truncates any
    packet whose declared length would exceed the report boundary.

    Args:
        report: Exactly 64 bytes read from the HID device.

    Returns:
        List of :class:`Packet` instances in order of appearance.

    """
    i = 0
    packets: list[Packet] = []
    while i + 1 < len(report):
        pkt_type = report[i]
        if pkt_type == 0x00:
            break

        length = report[i + 1]
        end = i + 2 + length
        if end > len(report):
            break

        packets.append(Packet(type=pkt_type, data=bytes(report[i + 2 : end])))
        i = end

    return packets


# ---------------------------------------------------------------------------
# Outbound packet builders - unauthenticated (safe, monitoring only)
# ---------------------------------------------------------------------------


def cmd_heartbeat() -> Packet:
    """
    Build the link-keepalive packet ``52 01 02``.

    Sent every :data:`HEARTBEAT_INTERVAL` seconds by the reader thread.
    Works unauthenticated; no PIN involved.
    """
    return Packet(PacketType.COMMAND, bytes([Command.HEARTBEAT]))


def cmd_get_sections_and_pg() -> Packet:
    """
    Build the sections + PG-outputs request ``52 01 0e``.

    Sent once at startup. After that the panel pushes 0x51 / 0x50
    automatically on every state change.
    """
    return Packet(PacketType.COMMAND, bytes([Command.GET_SECTIONS_AND_PG]))


def cmd_enable_device_states(minutes: int = ENABLE_DEV_STATES_MINUTES) -> Packet:
    """
    Build the enable-device-state-push request ``52 02 13 <minutes>``.

    Sent at startup and refreshed every :data:`ENABLE_DEV_STATES_INTERVAL`
    seconds. Instructs the panel to push 0x55 / 0xd8 device events for
    the next ``minutes`` minutes.

    Args:
        minutes: How long the panel should push device events. Defaults
            to :data:`ENABLE_DEV_STATES_MINUTES` (5).

    """
    return Packet(PacketType.COMMAND, bytes([Command.ENABLE_DEV_STATES, minutes]))


def cmd_get_system_info(kind: SysInfoType) -> Packet:
    """
    Build a system-info query ``30 01 <kind>``.

    Args:
        kind: Which system-info field to request.

    """
    return Packet(PacketType.GET_SYS_INFO, bytes([kind]))


# ---------------------------------------------------------------------------
# Outbound packet builders - authenticated (command path only)
# ---------------------------------------------------------------------------


class CodeError(ValueError):
    """Raised when a PIN string fails length validation."""

    def __init__(self, length: int) -> None:
        """Store actual length and build a message."""
        self.length = length
        super().__init__(
            f"PIN must be {CODE_MIN_LENGTH}-{CODE_MAX_LENGTH} digits; got {length}"
        )


def ui_authorisation_end() -> Packet:
    """
    Build the AUTH_END packet ``80 01 01``.

    Sent before every login attempt (to clear any stale session) and
    immediately after the MODIFY_SECTION ACK (to close the session).
    """
    return Packet(PacketType.UI_CONTROL, bytes([UiControl.AUTHORISATION_END]))


def ui_authorisation_code(prefix: str, code: str) -> Packet:
    """
    Build the AUTH_CODE login packet ``80 <n> 03 <prefix+code as ASCII>``.

    Wire format is plain ASCII: the subtype byte 0x03 followed by the
    concatenation of ``prefix`` and ``code`` encoded as ASCII digits.

    Verified by USB capture:
    - User 999 / PIN 1234 → ``03 39 39 39 31 32 33 34``
    - User 001 / PIN 1234 → ``03 30 30 31 31 32 33 34`` (wrong code)

    Args:
        prefix: 3-digit user-index prefix (e.g. ``"999"`` for the master
            / service user, ``"001"`` for user 1).
        code: PIN string, :data:`CODE_MIN_LENGTH` to :data:`CODE_MAX_LENGTH`
            decimal digits.

    Raises:
        CodeError: If ``code`` length is outside the allowed range.

    """
    if not CODE_MIN_LENGTH <= len(code) <= CODE_MAX_LENGTH:
        raise CodeError(len(code))

    payload = bytes([UiControl.AUTHORISATION_CODE]) + (prefix + code).encode("ascii")
    return Packet(PacketType.UI_CONTROL, payload)


def ui_modify_section(section: int, mode: ArmMode) -> Packet:
    """
    Build the MODIFY_SECTION command ``80 02 0d <byte>``.

    The command byte is ``mode + section`` where ``mode`` is the base
    from :class:`ArmMode`.

    Pcap-verified:
    - ``ArmMode.ARM_AWAY``, section 2 → ``0d a1``  (0x9f + 2 = 0xa1)
    - ``ArmMode.DISARM``,   section 2 → ``0d 91``  (0x8f + 2 = 0x91)

    Sections 1 and 3 and ARM_HOME / ARM_NIGHT are formula-derived and
    need a service-window test to confirm.

    Args:
        section: Section number (1-based).
        mode: The desired arm state.

    """
    return Packet(
        PacketType.UI_CONTROL,
        bytes([UiControl.MODIFY_SECTION, mode + section]),
    )


def ui_toggle_pg_output(output: int) -> Packet:
    """
    Build the TOGGLE_PG_OUTPUT command ``80 02 23 <output>``.

    Args:
        output: PG output number (1-based).

    """
    return Packet(
        PacketType.UI_CONTROL,
        bytes([UiControl.TOGGLE_PG_OUTPUT, output]),
    )


def ui_export_config() -> Packet:
    """
    Build the EXPORT_CONFIG trigger ``80 01 0f``.

    Triggers the panel to export its configuration to the FLEXI_CFG
    mass storage volume. Requires an active authenticated session with
    service/installer permissions (ffffffff). The panel responds with
    COMMAND_ACK (``80 02 1a 0a``) followed by EXPORT_DONE (``80 01 12``)
    after ~900ms.
    """
    return Packet(PacketType.UI_CONTROL, bytes([UiControl.EXPORT_CONFIG]))


def cmd_query_device_status(device_number: int) -> Packet:
    """
    Build the device status query ``52 02 28 <device_number>``.

    Requires an active authenticated session. The panel responds with
    a ``52 xx a8 <device_number> ...`` packet containing signal
    strength and battery level for wireless devices.

    Args:
        device_number: 0-based device number.

    """
    return Packet(
        PacketType.COMMAND,
        bytes([Command.QUERY_DEVICE_STATUS, device_number]),
    )


def cmd_diagnostics_start(device_number: int) -> Packet:
    """
    Build the diagnostics start command ``94 02 [dev] 01``.

    Starts diagnostics mode for a bus device. Must be followed by
    :func:`cmd_diagnostics_force_info` and eventually
    :func:`cmd_diagnostics_stop`.

    Requires an active authenticated session. Only works for bus devices.
    """
    return Packet(
        PacketType.DIAGNOSTICS,
        bytes([device_number, 0x01]),
    )


def cmd_diagnostics_force_info(device_number: int) -> Packet:
    """
    Build the force-info command ``96 03 [dev] 09 00``.

    Requests an info report from the bus device. The panel responds
    with a ``90 [len] [dev] 0a [payload...]`` packet.
    """
    return Packet(
        PacketType.DIAGNOSTICS_COMMAND,
        bytes([device_number, 0x09, 0x00]),
    )


def cmd_diagnostics_stop(device_number: int) -> Packet:
    """
    Build the diagnostics stop command ``94 02 [dev] 00``.

    Stops diagnostics mode for the device. Must be called after
    the info response is received.
    """
    return Packet(
        PacketType.DIAGNOSTICS,
        bytes([device_number, 0x00]),
    )


# Minimum bytes needed for a two-field payload (skip byte + at least one data byte)
_MIN_BITMAP_BYTES: int = 2
# Minimum bytes for a status packet (subtype + reason)
_MIN_STATUS_BYTES: int = 2

# Priority-ordered flag masks for deriving secondary state from byte 1.
# First match wins. Checked in descending priority order.
_SECONDARY_FLAGS: tuple[tuple[int, SectionSecondaryState], ...] = (
    (0x18, SectionSecondaryState.TRIGGERED),  # bits 4+3
    (0x40, SectionSecondaryState.PENDING),  # bit 6
    (0x80, SectionSecondaryState.ARMING),  # bit 7
)


def decode_sections(data: bytes) -> list[SectionState]:
    """
    Decode the payload of a :attr:`PacketType.SECTIONS` (0x51) packet.

    The panel sends two-byte slots followed by a trailer. Each slot
    encodes one section as a single state byte + flags byte.

    State byte layout::

        bit 7:      arming (exit delay)
        bit 6:      pending (entry delay)
        bits 4+3:   triggered
        bits [2:0]: primary state (see :class:`SectionPrimaryState`)

    Secondary state is derived from flag bits via :data:`_SECONDARY_FLAGS`
    priority table. Primary state uses bits [2:0].

    Slots with primary :attr:`SectionPrimaryState.OFF` or
    :attr:`SectionPrimaryState.UNSET` are skipped (not active sections).

    Args:
        data: Raw DATA bytes from the 0x51 TLV atom.

    Returns:
        List of :class:`SectionState` for active sections only, in
        section-number order (1-based).

    """
    states: list[SectionState] = []
    for idx in range(0, len(data) - 1, 2):
        raw_byte = data[idx]
        flags = data[idx + 1]

        primary = SectionPrimaryState(raw_byte & 0x07)
        secondary = SectionSecondaryState.NORMAL
        for mask, state in _SECONDARY_FLAGS:
            if raw_byte & mask:
                secondary = state
                break

        if primary in (SectionPrimaryState.UNSET, SectionPrimaryState.OFF):
            continue

        states.append(
            SectionState(
                number=idx // 2 + 1,
                primary=primary,
                secondary=secondary,
                flags=flags,
            )
        )

    return states


def decode_devices_states(data: bytes) -> DeviceActivity:
    """
    Decode the payload of a :attr:`PacketType.DEVICES_STATES` (0xd8) packet.

    Per pcap analysis: the first byte is skipped; the remaining bytes
    form a little-endian integer where bit N corresponds to device N
    (bit 0 unused).

    Args:
        data: Raw DATA bytes from the 0xd8 TLV atom.

    Returns:
        :class:`DeviceActivity` with the set of active device numbers.

    """
    if len(data) < _MIN_BITMAP_BYTES:
        return DeviceActivity(active=frozenset())

    bitmap_bytes = data[1:]
    value = int.from_bytes(bitmap_bytes, byteorder="little")
    active = frozenset(i for i in range(1, len(bitmap_bytes) * 8) if value & (1 << i))
    return DeviceActivity(active=active)


def decode_system_info(data: bytes) -> SystemInfo | None:
    """
    Decode the payload of a :attr:`PacketType.SYS_INFO` (0x40) packet.

    Args:
        data: Raw DATA bytes from the 0x40 TLV atom.

    Returns:
        :class:`SystemInfo` or ``None`` if ``data`` is empty or the
        type byte is unrecognised.

    """
    if not data:
        return None

    kind_raw = data[0]
    try:
        kind = SysInfoType(kind_raw)
    except ValueError:
        return None

    payload = data[1:]
    if kind == SysInfoType.MAC:
        value = ":".join(f"{b:02x}" for b in payload[:6])
    else:
        value = payload.split(b"\x00")[0].decode("ascii", errors="replace")

    return SystemInfo(kind=kind, value=value)


def decode_ui_status(data: bytes) -> UiStatusEvent | None:
    """
    Decode the payload of a :attr:`UiControl.STATUS` packet (``80 1b ...``).

    The caller is expected to check that the first data byte is
    :attr:`UiControl.STATUS` (0x1b) before calling this.

    Args:
        data: Raw DATA bytes from a 0x80 TLV atom whose first byte is
            0x1b.

    Returns:
        :class:`UiStatusEvent` or ``None`` if ``data`` is too short.

    """
    if len(data) < _MIN_STATUS_BYTES:
        return None

    return UiStatusEvent(reason=data[1], raw=bytes(data))


def decode_device_status(data: bytes) -> DeviceStatus | None:
    """
    Decode the payload of a device status response (``52 xx a8 ...``).

    The caller passes the full DATA bytes of the 0x52 TLV atom.
    The first byte must be :attr:`Command.QUERY_DEVICE_STATUS_RESPONSE`
    (0xa8).

    Response formats::

        Wired:    a8 [dev] [flags] [b2] [act_hi] [act_lo] 00 f2 [state]
        Wireless: a8 [dev] [flags] [b2] [act_hi] [act_lo] [ex] fc [sig] [bat] 00

    Args:
        data: Raw DATA bytes from a 0x52 TLV atom whose first byte is 0xa8.

    Returns:
        :class:`DeviceStatus` or ``None`` if the data is malformed.

    """
    if len(data) < _MIN_DEVICE_STATUS_BYTES:
        return None

    if data[0] != Command.QUERY_DEVICE_STATUS_RESPONSE:
        return None

    device_number = data[1]

    # Find the marker byte to determine response format.
    fc_idx = data.find(_WIRELESS_MARKER, 2)
    f2_idx = data.find(_WIRED_MARKER, 2)

    if fc_idx > 0 and len(data) > fc_idx + 2:
        # Wireless device: fc [signal] [battery] 00
        signal_byte = data[fc_idx + 1]
        battery_byte = data[fc_idx + 2]

        signal = _decode_signal(signal_byte)
        battery = _decode_battery(battery_byte)

        # Activity: 0xFF means never communicated.
        active = (
            len(data) > _ACTIVITY_OFFSET
            and data[_ACTIVITY_OFFSET] != _ACTIVITY_NEVER
            and data[_ACTIVITY_OFFSET] != 0x00
        )

        return DeviceStatus(
            device_number=device_number,
            signal=signal,
            battery=battery,
            active=active,
        )

    if f2_idx > 0:
        # Wired device: f2 [state_byte]
        # Activity check: bytes 4-5 (activity_hi, activity_lo).
        active = len(data) > _WIRED_ACTIVITY_END and (
            data[_ACTIVITY_OFFSET] != 0x00 or data[_WIRED_ACTIVITY_END] != 0x00
        )

        return DeviceStatus(
            device_number=device_number,
            signal=None,
            battery=None,
            active=active,
        )

    # Unknown format - return minimal info.
    return DeviceStatus(
        device_number=device_number,
        signal=None,
        battery=None,
        active=False,
    )


def _decode_signal(byte: int) -> int:
    """
    Decode a signal strength byte to a percentage.

    Formula not fully confirmed for 0xa8 responses. Using lower 5 bits
    times 5 as a reasonable approximation. Clamped to 0-100.
    """
    raw = (byte & 0x1F) * 5
    return min(raw, _BATTERY_MAX_PERCENT)


def _decode_battery(byte: int) -> int | None:
    """
    Decode a battery level byte to a percentage.

    Lower nibble * 10 = percentage. Special values 0x0b-0x0f are
    not real battery levels (no change, external power, etc.).

    Returns:
        Battery percentage (0-100), or ``None`` for special values.

    """
    if byte >= _BATTERY_SPECIAL_THRESHOLD:
        return None

    level = (byte & 0x0F) * _BATTERY_LEVEL_STEP
    return min(level, _BATTERY_MAX_PERCENT)


def decode_device_diagnostic(data: bytes) -> DeviceDiagnostic | None:
    """
    Decode a 0x90 diagnostics response.

    Response format::

        [dev_num] 0a [payload_len] [flags] [signal] [rest...]

    Flags byte doubles as battery indicator (same encoding as wireless):
    - < 0x0b: battery percentage (lower nibble * 10)
    - = 0x0a: siren (voltage in payload as uint16 LE centivolts)
    - >= 0x0b: bus-powered, no battery

    Args:
        data: Raw DATA bytes from a 0x90 TLV atom.

    Returns:
        :class:`DeviceDiagnostic` or ``None`` if malformed.

    """
    # Minimum: dev_num + 0x0a + payload_len + flags + signal = 5 bytes
    if len(data) < _MIN_BUS_INFO_BYTES:
        return None

    device_number = data[0]
    subtype = data[1]

    if subtype != _DEVICE_INFO_REQUESTED:
        return None

    flags = data[3]
    signal_raw = (
        data[_BUS_INFO_SIGNAL_OFFSET] if len(data) > _BUS_INFO_SIGNAL_OFFSET else 0
    )
    signal = min((signal_raw & 0x1F) * 5, 100)
    rest = data[5:]

    # Parse battery based on flags.
    battery: int | None = None
    voltage: float | None = None
    voltage_current: float | None = None

    if flags == _SIREN_FLAGS:
        # Siren: voltages in payload as 6c 00 [v1_LE] 6c 01 [v2_LE]
        voltage, voltage_current = _parse_siren_voltages(rest)
    elif flags < _BATTERY_SPECIAL_THRESHOLD:
        # Battery percentage from flags byte (same as wireless encoding).
        battery = (flags & 0x0F) * _BATTERY_LEVEL_STEP

    return DeviceDiagnostic(
        device_number=device_number,
        signal=signal,
        battery=battery,
        voltage=voltage,
        voltage_current=voltage_current,
    )


# Siren flags value.
_SIREN_FLAGS: int = 0x0A
# Siren voltage marker byte.
_SIREN_VOLTAGE_MARKER: int = 0x6C


def _parse_siren_voltages(rest: bytes) -> tuple[float | None, float | None]:
    """
    Parse both battery voltages from siren diagnostic payload.

    Format: ``6c 00 [v1_LE_uint16] 6c 01 [v2_LE_uint16]``
    Voltages are in centivolts (330 = 3.30V).

    Returns:
        Tuple of (voltage, voltage_current).

    """
    v1: float | None = None
    v2: float | None = None
    idx = 0
    while idx < len(rest) - 3:
        if rest[idx] == _SIREN_VOLTAGE_MARKER:
            cv = int.from_bytes(rest[idx + 2 : idx + 4], "little")
            if rest[idx + 1] == 0x00:
                v1 = cv / 100.0
            elif rest[idx + 1] == 0x01:
                v2 = cv / 100.0

            idx += 4
        else:
            idx += 1

    return v1, v2
