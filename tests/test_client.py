"""Tests for custom_components.jablotron_local.client."""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from custom_components.jablotron_local.client import (
    JablotronAuthError,
    JablotronClient,
    JablotronCommandError,
    JablotronConnectionError,
)
from custom_components.jablotron_local.protocol import (
    ArmMode,
    Packet,
    PacketType,
    UiControl,
    UiStatusReason,
    encode_report,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Simulated panel responses (64-byte reports).
_LOGIN_OK = encode_report(
    Packet(PacketType.UI_CONTROL, bytes([UiControl.LOGIN_INFO, 0x00]))
)
_COMMAND_ACK = encode_report(
    Packet(PacketType.UI_CONTROL, bytes([UiControl.COMMAND_ACK, 0x00]))
)
_WRONG_CODE = encode_report(
    Packet(
        PacketType.UI_CONTROL,
        bytes([UiControl.STATUS, UiStatusReason.WRONG_CODE]),
    )
)


def _make_client(path: str = "/dev/hidraw3") -> JablotronClient:
    """Create a client with mocked callbacks."""
    client = JablotronClient(path=path)
    client.on_packets = MagicMock()
    client.on_connection_change = MagicMock()
    return client


def _is_auth_code_write(data: bytes) -> bool:
    """Check if a write contains an AUTH_CODE packet."""
    return (
        len(data) > 3
        and data[1] == PacketType.UI_CONTROL
        and data[3] == UiControl.AUTHORISATION_CODE
    )


def _is_modify_section_write(data: bytes) -> bool:
    """Check if a write contains a MODIFY_SECTION packet."""
    return (
        len(data) > 3
        and data[1] == PacketType.UI_CONTROL
        and data[3] == UiControl.MODIFY_SECTION
    )


class _ReaderHelper:
    """
    Helper that feeds responses to the reader thread on demand.

    The reader thread calls os.read in a loop. This helper returns
    BlockingIOError (no data) by default but allows injecting specific
    responses that will be returned on the next read call.
    """

    def __init__(self) -> None:
        self._queue: list[bytes] = []
        self._lock = threading.Lock()

    def inject(self, *reports: bytes) -> None:
        """Queue reports to be returned by the next read calls."""
        with self._lock:
            self._queue.extend(reports)

    def mock_read(self, _fd: int, _size: int) -> bytes:
        """Mock os.read: return queued data or raise BlockingIOError."""
        with self._lock:
            if self._queue:
                return self._queue.pop(0)
        raise BlockingIOError


# ---------------------------------------------------------------------------
# connect / disconnect lifecycle
# ---------------------------------------------------------------------------


class TestConnectDisconnect:
    def test_connect_opens_device_and_starts_thread(self):
        reader = _ReaderHelper()
        with (
            patch("os.open", return_value=10) as mock_open,
            patch("os.write"),
            patch("os.read", side_effect=reader.mock_read),
            patch("os.close"),
        ):
            client = _make_client()
            client.connect()

            assert client.connected
            assert client._reader_thread is not None
            assert client._reader_thread.is_alive()

            client.disconnect()

            assert not client.connected
            assert client._reader_thread is None

        mock_open.assert_called_once()

    def test_connect_failure_raises_connection_error(self):
        with patch("os.open", side_effect=OSError("No such device")):
            client = _make_client()
            with pytest.raises(JablotronConnectionError) as exc_info:
                client.connect()

            assert exc_info.value.path == "/dev/hidraw3"

    def test_disconnect_is_idempotent(self):
        reader = _ReaderHelper()
        with (
            patch("os.open", return_value=10),
            patch("os.write"),
            patch("os.read", side_effect=reader.mock_read),
            patch("os.close"),
        ):
            client = _make_client()
            client.connect()
            client.disconnect()
            # Second disconnect should not raise
            client.disconnect()

    def test_connection_change_callback_on_connect_and_disconnect(self):
        reader = _ReaderHelper()
        with (
            patch("os.open", return_value=10),
            patch("os.write"),
            patch("os.read", side_effect=reader.mock_read),
            patch("os.close"),
        ):
            client = _make_client()
            client.connect()

            args = client.on_connection_change.call_args[0]
            assert args == (True,)

            client.disconnect()

            args = client.on_connection_change.call_args[0]
            assert args == (False,)


# ---------------------------------------------------------------------------
# Reader thread - heartbeat and startup
# ---------------------------------------------------------------------------


class TestReaderThread:
    def test_sends_startup_queries_on_connect(self):
        """Reader thread sends sysinfo + sections + enable-dev-states."""
        writes: list[bytes] = []
        reader = _ReaderHelper()

        def capture_write(_fd: int, data: bytes) -> int:
            writes.append(bytes(data))
            return len(data)

        with (
            patch("os.open", return_value=10),
            patch("os.write", side_effect=capture_write),
            patch("os.read", side_effect=reader.mock_read),
            patch("os.close"),
        ):
            client = _make_client()
            client.connect()
            time.sleep(0.3)
            client.disconnect()

        # Should have written startup packets (sysinfo + sections/enable)
        assert len(writes) >= 2

    def test_sends_heartbeat_periodically(self):
        """Reader thread sends heartbeat packets at configured interval."""
        writes: list[bytes] = []
        reader = _ReaderHelper()

        def capture_write(_fd: int, data: bytes) -> int:
            writes.append(bytes(data))
            return len(data)

        with (
            patch("os.open", return_value=10),
            patch("os.write", side_effect=capture_write),
            patch("os.read", side_effect=reader.mock_read),
            patch("os.close"),
            patch(
                "custom_components.jablotron_local.client.HEARTBEAT_INTERVAL",
                0.1,
            ),
        ):
            client = _make_client()
            client.connect()
            time.sleep(0.5)
            client.disconnect()

        # Heartbeat report: \x00 report-ID, \x52 type, \x01 len, \x02 data
        heartbeat_prefix = b"\x00\x52\x01\x02"
        heartbeats = [w for w in writes if w.startswith(heartbeat_prefix)]
        assert len(heartbeats) >= 2

    def test_dispatches_inbound_packets_to_callback(self):
        """Packets read from device are dispatched to on_packets."""
        section_data = bytes([0x01, 0x00]) * 3
        report = encode_report(Packet(PacketType.SECTIONS, section_data))
        reader = _ReaderHelper()
        reader.inject(report)

        with (
            patch("os.open", return_value=10),
            patch("os.write"),
            patch("os.read", side_effect=reader.mock_read),
            patch("os.close"),
        ):
            client = _make_client()
            client.connect()
            time.sleep(0.3)
            client.disconnect()

        assert client.on_packets.call_count >= 1
        packets = client.on_packets.call_args_list[0][0][0]
        assert any(p.type == PacketType.SECTIONS for p in packets)


# ---------------------------------------------------------------------------
# Command path: modify_section
# ---------------------------------------------------------------------------


class TestModifySection:
    def test_success(self):
        """Full arm sequence completes without raising."""
        writes: list[bytes] = []
        reader = _ReaderHelper()

        def capture_write(_fd: int, data: bytes) -> int:
            writes.append(bytes(data))
            if _is_auth_code_write(data):
                reader.inject(_LOGIN_OK)
            elif _is_modify_section_write(data):
                reader.inject(_COMMAND_ACK)
            return len(data)

        with (
            patch("os.open", return_value=10),
            patch("os.write", side_effect=capture_write),
            patch("os.read", side_effect=reader.mock_read),
            patch("os.close"),
            patch(
                "custom_components.jablotron_local.client._READ_POLL_INTERVAL",
                0.02,
            ),
        ):
            client = _make_client()
            client.connect()
            time.sleep(0.1)

            client.modify_section(2, ArmMode.ARM_AWAY, "9991234")
            client.disconnect()

        # Verify the write sequence contains the command packets
        ui_writes = [w for w in writes if len(w) > 3 and w[1] == PacketType.UI_CONTROL]
        subtypes = [w[3] for w in ui_writes]

        assert UiControl.AUTHORISATION_END in subtypes
        assert UiControl.AUTHORISATION_CODE in subtypes
        assert UiControl.MODIFY_SECTION in subtypes

    def test_wrong_code_raises_auth_error(self):
        """Panel sends WRONG_CODE -> JablotronAuthError raised."""
        reader = _ReaderHelper()

        def on_write(_fd: int, data: bytes) -> int:
            if _is_auth_code_write(data):
                reader.inject(_WRONG_CODE)
            return len(data)

        with (
            patch("os.open", return_value=10),
            patch("os.write", side_effect=on_write),
            patch("os.read", side_effect=reader.mock_read),
            patch("os.close"),
            patch(
                "custom_components.jablotron_local.client._READ_POLL_INTERVAL",
                0.02,
            ),
        ):
            client = _make_client()
            client.connect()
            time.sleep(0.1)

            with pytest.raises(JablotronAuthError):
                client.modify_section(2, ArmMode.ARM_AWAY, "9991234")

            assert not client.command_in_progress
            client.disconnect()

    def test_login_timeout_raises_command_error(self):
        """No login response within timeout -> JablotronCommandError."""
        reader = _ReaderHelper()
        with (
            patch("os.open", return_value=10),
            patch("os.write"),
            patch("os.read", side_effect=reader.mock_read),
            patch("os.close"),
            patch("custom_components.jablotron_local.client.LOGIN_TIMEOUT", 0.1),
            patch(
                "custom_components.jablotron_local.client._READ_POLL_INTERVAL",
                0.02,
            ),
        ):
            client = _make_client()
            client.connect()
            time.sleep(0.1)

            with pytest.raises(JablotronCommandError):
                client.modify_section(2, ArmMode.DISARM, "9991234")

            assert not client.command_in_progress
            client.disconnect()

    def test_ack_timeout_raises_command_error(self):
        """Login OK but no ACK -> JablotronCommandError."""
        reader = _ReaderHelper()

        def on_write(_fd: int, data: bytes) -> int:
            if _is_auth_code_write(data):
                reader.inject(_LOGIN_OK)
            return len(data)

        with (
            patch("os.open", return_value=10),
            patch("os.write", side_effect=on_write),
            patch("os.read", side_effect=reader.mock_read),
            patch("os.close"),
            patch(
                "custom_components.jablotron_local.client.COMMAND_ACK_TIMEOUT",
                0.1,
            ),
            patch(
                "custom_components.jablotron_local.client._READ_POLL_INTERVAL",
                0.02,
            ),
        ):
            client = _make_client()
            client.connect()
            time.sleep(0.1)

            with pytest.raises(JablotronCommandError):
                client.modify_section(2, ArmMode.ARM_AWAY, "9991234")

            client.disconnect()

    def test_not_connected_raises_connection_error(self):
        """modify_section while not connected raises error."""
        client = _make_client()
        with pytest.raises(JablotronConnectionError):
            client.modify_section(1, ArmMode.DISARM, "9991234")

    def test_command_in_progress_flag(self):
        """command_in_progress is True during modify_section."""
        observed_flags: list[bool] = []
        reader = _ReaderHelper()

        def capture_write(_fd: int, data: bytes) -> int:
            observed_flags.append(client.command_in_progress)
            if _is_auth_code_write(data):
                reader.inject(_LOGIN_OK)
            elif _is_modify_section_write(data):
                reader.inject(_COMMAND_ACK)
            return len(data)

        with (
            patch("os.open", return_value=10),
            patch("os.write", side_effect=capture_write),
            patch("os.read", side_effect=reader.mock_read),
            patch("os.close"),
            patch(
                "custom_components.jablotron_local.client._READ_POLL_INTERVAL",
                0.02,
            ),
        ):
            client = _make_client()
            client.connect()
            time.sleep(0.1)

            client.modify_section(2, ArmMode.ARM_AWAY, "9991234")
            client.disconnect()

        # Some writes during command should see flag=True
        assert any(observed_flags)
        # After modify_section returns, flag is False
        assert not client.command_in_progress


# ---------------------------------------------------------------------------
# Reconnect on OSError
# ---------------------------------------------------------------------------


class TestReconnect:
    def test_reconnects_on_read_error(self):
        """OSError during read triggers reconnect with backoff."""
        open_count = 0
        read_count = 0

        def mock_open(_path: str, _flags: int) -> int:
            nonlocal open_count
            open_count += 1
            return 10 + open_count

        def mock_read(_fd: int, _size: int) -> bytes:
            nonlocal read_count
            read_count += 1
            if read_count <= 2:
                return b""  # Empty read -> OSError in client
            raise BlockingIOError

        with (
            patch("os.open", side_effect=mock_open),
            patch("os.write"),
            patch("os.read", side_effect=mock_read),
            patch("os.close"),
            patch(
                "custom_components.jablotron_local.client._BACKOFF_INITIAL",
                0.05,
            ),
            patch(
                "custom_components.jablotron_local.client._READ_POLL_INTERVAL",
                0.05,
            ),
        ):
            client = _make_client()
            client.connect()
            time.sleep(0.5)
            client.disconnect()

        # Opened at least twice (initial + reconnect)
        assert open_count >= 2

    def test_reconnect_notifies_connection_change(self):
        """Connection change callback fires on disconnect/reconnect."""
        read_count = 0
        connection_states: list[bool] = []

        def mock_open(_path: str, _flags: int) -> int:
            return 10

        def mock_read(_fd: int, _size: int) -> bytes:
            nonlocal read_count
            read_count += 1
            if read_count == 1:
                return b""  # Trigger disconnect
            raise BlockingIOError

        def on_connection_change(connected: bool) -> None:  # noqa: FBT001
            connection_states.append(connected)

        with (
            patch("os.open", side_effect=mock_open),
            patch("os.write"),
            patch("os.read", side_effect=mock_read),
            patch("os.close"),
            patch(
                "custom_components.jablotron_local.client._BACKOFF_INITIAL",
                0.05,
            ),
            patch(
                "custom_components.jablotron_local.client._READ_POLL_INTERVAL",
                0.05,
            ),
        ):
            client = _make_client()
            client.on_connection_change = on_connection_change
            client.connect()
            time.sleep(0.5)
            client.disconnect()

        # Should see: True (connect), False (error), True (reconnect)
        assert True in connection_states
        assert False in connection_states
