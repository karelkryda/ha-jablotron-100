# Contributing to Jablotron Local

## Development setup

1. Clone the repository
2. Open in VS Code with the Dev Containers extension
3. The container will install all dependencies automatically via `scripts/setup`
4. Run `scripts/develop` to start Home Assistant with the integration loaded

**Note:** The devcontainer passes `/dev/hidraw0` into the container for live panel testing. The panel must be attached via USB (or usbip) before opening the container.

## Code standards

- All code must pass `ruff check` with the project's strict ALL config
- All code must pass `ruff format`
- Python 3.14+ (no `from __future__ import annotations`)
- Docstrings on all public classes and methods
- Type annotations on all function signatures

## Project structure

```
custom_components/jablotron_local/
  __init__.py             - Integration setup and teardown
  alarm_control_panel.py  - Alarm control panel entities
  client.py               - USB HID client (blocking reader thread + commands)
  config_flow.py          - UI configuration flow (USB discovery + manual)
  const.py                - Integration constants (domain, USB VID/PID)
  coordinator.py          - Push-style DataUpdateCoordinator
  data.py                 - Runtime data types
  diagnostics.py          - Diagnostics download for bug reports
  hidraw.py               - HID device enumeration and probing
  protocol.py             - TLV framing and packet codec (pure, no I/O)
  manifest.json           - Integration manifest
  translations/           - UI strings

tests/
  conftest.py             - Shared fixtures
  test_config_flow.py     - Config flow tests
  test_protocol.py        - Protocol codec tests (pcap-verified vectors)
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
