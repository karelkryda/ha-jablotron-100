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

COMMAND (per arm/disarm, authenticated window ~20 ms)
    Sequence verified against USB capture from JA-Link:

        80 01 01          AUTH_END  (clear stale session)
        80 <n> 03 <ascii> AUTH_CODE (prefix + pin, ASCII)
        ...wait for 80 0c (ok) or 80 1b 03 (wrong code)...
        80 02 0d <byte>   MODIFY_SECTION
        ...wait for 80 1a (ACK)...
        80 01 01          AUTH_END  (logout)

Code encoding (pcap-verified)
------------------------------
Wire format is: subtype 0x03 followed by ``(prefix + pin).encode('ascii')``.
For example, user 999, PIN 1234 gives ``03 39 39 39 31 32 33 34``.
This matches kukulich's ``magic_offset=48 + int(digit)`` formula since
``chr(48 + d) == str(d)`` for d in 0..9. His hardcoded ``b'\x39\x39\x39'``
is just ``b'999'`` as ASCII. Both produce identical wire bytes.

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

# ---------------------------------------------------------------------------
# Timing (seconds) - use these in client.py, not magic numbers
# ---------------------------------------------------------------------------

HEARTBEAT_INTERVAL: float = 1.0
ENABLE_DEV_STATES_INTERVAL: float = 60.0
ENABLE_DEV_STATES_MINUTES: int = 5
LOGIN_TIMEOUT: float = 2.0
COMMAND_ACK_TIMEOUT: float = 2.0
STATE_CONFIRM_TIMEOUT: float = 5.0


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
    DEVICE_INFO = 0x90  # IN: device details
    DEVICES_STATES = 0xD8  # IN: activity bitmap (little-endian)


class Command(IntEnum):
    """Subtype byte for :attr:`PacketType.COMMAND` packets (OUT)."""

    HEARTBEAT = 0x02  # 52 01 02 - link keepalive, unauthenticated
    GET_DEVICE_STATUS = 0x0A  # 52 02 0a <n> - request device info
    GET_SECTIONS_AND_PG = 0x0E  # 52 01 0e - request current sections + PG
    ENABLE_DEV_STATES = 0x13  # 52 02 13 <minutes>


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
    # Kukulich's labels are swapped; verified against capture:
    # type 0x08 → "MD6112.07.0" = firmware string
    # type 0x09 → "MD15005"     = hardware string
    FIRMWARE = 0x08
    HARDWARE = 0x09
    REG_CODE = 0x0A
    NAME = 0x0B
    MAC = 0x0C


class SectionPrimaryState(IntEnum):
    """
    Primary state of a section.

    Taken from the first byte of each 2-byte slot in a
    :attr:`PacketType.SECTIONS` payload.
    """

    DISARMED = 1
    ARMED_PARTIAL = 2
    ARMED_FULL = 3
    MAINTENANCE = 4
    SERVICE = 5
    BLOCKED = 6
    OFF = 7  # slot unused / section not configured


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
    ARM_NIGHT = 0xAF  # base: same as ARM_HOME per kukulich, not yet verified


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
        flags: Secondary flags byte (semantics partially unknown; 0x00
            in normal operation).

    """

    number: int
    primary: SectionPrimaryState
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

    This is equivalent to kukulich's ``magic_offset=48 + int(digit)``
    formula since ``48 + d == ord(str(d))`` for d in 0..9, but expressed
    more directly.

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


# Minimum bytes needed for a two-field payload (skip byte + at least one data byte)
_MIN_BITMAP_BYTES: int = 2
# Minimum bytes for a status packet (subtype + reason)
_MIN_STATUS_BYTES: int = 2


def decode_sections(data: bytes) -> list[SectionState]:
    """
    Decode the payload of a :attr:`PacketType.SECTIONS` (0x51) packet.

    The panel sends 16 two-byte slots followed by a two-byte status
    trailer (0x00 0x94 observed). Each slot is ``primary_state, flags``.
    Slots with primary state :attr:`SectionPrimaryState.OFF` (7) or
    outside 1..7 are not active sections and are skipped.

    Args:
        data: Raw DATA bytes from the 0x51 TLV atom.

    Returns:
        List of :class:`SectionState` for active sections only, in
        section-number order (1-based).

    """
    states: list[SectionState] = []
    for idx in range(0, len(data) - 1, 2):
        primary_raw = data[idx]
        flags = data[idx + 1]
        if primary_raw not in range(1, 8) or primary_raw == SectionPrimaryState.OFF:
            continue

        states.append(
            SectionState(
                number=idx // 2 + 1,
                primary=SectionPrimaryState(primary_raw),
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
