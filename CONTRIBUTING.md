# Contributing to Jablotron Local

## Development setup

1. Clone the repository
2. Open in VS Code with the Dev Containers extension
3. The container will install all dependencies automatically via `scripts/setup`
4. Run `scripts/develop` to start Home Assistant with the integration loaded

**Note:** The devcontainer uses `--privileged` mode for USB device access. Ensure the panel's block devices are readable (`chmod a+r /dev/sdX`) before starting HA.

## Code standards

- All code must pass `ruff check` with the project's strict ALL config
- All code must pass `ruff format`
- Python 3.14+ (no `from __future__ import annotations`)
- Docstrings on all public classes and methods
- Type annotations on all function signatures

## Project structure

```
custom_components/jablotron_local/
  __init__.py                 - Integration setup (connect, config export, device probe)
  alarm_control_panel.py      - Alarm control panel entities (per section)
  binary_sensor.py            - Binary sensor entities (per device, from activity bitmap)
  sensor.py                   - Battery, signal, voltage sensor entities
  client.py                   - USB HID client (reader thread + authenticated commands)
  config_flow.py              - UI configuration flow (USB discovery + manual + reauth)
  config_reader.py            - Panel config reader (FLEXI_CFG mass storage)
  coordinator.py              - DataUpdateCoordinator (push + periodic poll)
  const.py                    - Integration constants (domain, USB VID/PID)
  data.py                     - Runtime data types
  diagnostics.py              - Diagnostics download for bug reports
  hidraw.py                   - HID device enumeration and probing
  protocol.py                 - TLV framing, packet codec, device status decoders
  manifest.json               - Integration manifest
  translations/               - UI strings

tests/
  conftest.py                 - Shared fixtures
  test_config_flow.py         - Config flow tests
  test_config_reader.py       - Config reader/parser tests
  test_device_status.py       - Device status/diagnostic decoder tests
  test_protocol.py            - Protocol codec tests (pcap-verified vectors)
  test_coordinator.py         - Coordinator tests
  test_client.py              - Client tests
  test_alarm_control_panel.py - Entity tests
  test_hidraw.py              - Hidraw enumeration tests
```

## Testing

```bash
# Run all tests
.venv/bin/pytest tests/

# Run with verbose output
.venv/bin/pytest tests/ -v

# Run only protocol tests
.venv/bin/pytest tests/test_protocol.py
```

Tests use `pytest-homeassistant-custom-component` for config flow tests and plain pytest for protocol codec tests.

## Protocol reverse engineering

The `protocol.py` module is verified against USB captures from JA-Link. Test vectors in `tests/test_protocol.py` use byte sequences extracted from these captures.

## Reporting issues

Please include:

- Your panel model (JA-101K, JA-103K, JA-106K, etc.)
- Home Assistant version
- Relevant log entries (enable debug logging for `custom_components.jablotron_local`)
