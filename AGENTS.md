# AGENTS.md

## Project Overview

Home Assistant custom integration for local control of Jablotron JA-100+ alarm panels via USB HID. Bypasses Jablonet cloud entirely using the panel's J-Link/F-Link USB port with raw Linux hidraw I/O (no native dependencies).

## Architecture

- `protocol.py` - TLV framing codec. Builds/splits 64-byte HID reports, packet constants (IntEnum with _missing_ fallback to UNKNOWN), encode/decode helpers, dataclasses for parsed data (DeviceStatus, DeviceDiagnostic, DeviceInfo, SectionState with primary/secondary states). Includes device status query (52 02 28), bus diagnostics (94/96), and response decoders. Pure functions, zero I/O. Fully unit-tested.
- `hidraw.py` - Low-level device infrastructure. Enumerates panels via sysfs, probes device accessibility (permissions, exclusive access). Prefers stable /dev symlinks over raw /dev/hidrawN paths when udev rules create them. Used by config_flow.
- `config_reader.py` - Panel configuration reader via FLEXI_CFG mass storage. Discovers block device via sysfs (same USB parent), reads sectors after authenticated export trigger, XOR 0xFF decrypts, parses section names and device entries (name, section, rf_byte0). Resolves symlinks for sysfs lookup.
- `client.py` - Blocking USB HID client. Background reader thread runs unauthenticated monitoring loop (heartbeat + enable device states). Three authenticated command paths serialized by `_session_lock`: `modify_section` (arm/disarm), `export_config` (trigger FLEXI_CFG write), `probe_all_devices` (query status + diagnostics). Called via `async_add_executor_job`.
- `coordinator.py` - DataUpdateCoordinator combining push and poll. Section states and device activity pushed by panel via reader thread. Device status (battery, signal, voltage) polled at configurable interval (default 30 min) via `_async_update_data`. Triggers reauth on unexpected WRONG_CODE. Logs unhandled packet types at DEBUG level for protocol discovery.
- `alarm_control_panel.py` - AlarmControlPanelEntity per active section. State from 0x51 push (primary + secondary mapped to HA states). Section names from panel config. Arm/disarm via per-user PIN.
- `binary_sensor.py` - BinarySensorEntity per device. State from 0xd8 activity bitmap. Device names from panel config.
- `sensor.py` - Battery (%), signal (%), and voltage (V) sensors per device. Data from DeviceInfo populated by startup probe and periodic refresh.
- `config_flow.py` - USB auto-discovery + manual sysfs enumeration + reconfigure + reauth flows. Probes device before entry creation. Accepts optional service PIN and probe interval.
- `const.py` - DOMAIN, LOGGER, USB VID/PID. Protocol constants live in protocol.py.
- `data.py` - Typed ConfigEntry runtime data (client + coordinator).
- `diagnostics.py` - Downloadable diagnostics for bug reports (serial redacted).

## Protocol Details

- Transport: USB HID, 64-byte interrupt reports (IN ep 0x81, OUT via SET_REPORT on ep 0x00)
- Framing: TLV - TYPE(1) | LEN(1) | DATA(LEN), concatenated and zero-padded to 64 bytes
- Multiple packets per report are common
- Device: VID 0x16D6 (Jablotron), PID 0x0008
- Linux path: /dev/hidraw\* (exclusive access - J-Link/F-Link must be closed)
- Raw fd I/O via os.open/read/write (no hidapi dependency)

## Key Design Decisions

- Monitoring is permanently UNAUTHENTICATED. The reader thread sends only code-free packets (heartbeat 52 01 02, enable device states 52 02 13 05, get sections 52 01 0e). It structurally cannot trip the alarm.
- PIN is emitted ONLY on explicit user arm/disarm: AUTH_END -> AUTH_CODE -> wait LOGIN_INFO -> MODIFY_SECTION -> wait ACK -> AUTH_END. Never stored, never on a timer, never on reconnect.
- Per-user attribution: "999" prefix = wildcard user lookup on the panel. Panel identifies the user from the PIN and logs them in its event log.
- Wrong code handling: panel sends 80 1b 03. Client raises JablotronAuthError, entity surfaces HomeAssistantError to the user. Immediate retry allowed (user-initiated, not a code loop).
- Push-style coordinator: sections and device activity pushed by the panel. Device status polled at configurable interval (default 30 min) via DataUpdateCoordinator.update_interval.
- Session lock (`_session_lock`): serializes all authenticated sessions (arm/disarm, config export, device probe). Only one session at a time - others block until the current one completes.
- `user_initiated_action` flag: True only during user arm/disarm. If WRONG_CODE arrives while False, the integration triggers reauthentication (bad service PIN or bug). Prevents silent PIN hammering.
- `service_pin_rejected` flag: set on first wrong code from internal action. Blocks all further authenticated operations until reconfigured. One bad attempt, never retry.
- Raw /dev/hidraw over hidapi: zero native dependencies, Linux-only (HA OS target). O_NONBLOCK reads + sleep loop in reader thread.
- Reader thread owns all reads. Command path signals via threading.Event (no fd contention).
- Auto-reconnect: exponential backoff (1s-60s) on USB errors. Re-sends startup sequence on reconnect.
- Config export: authenticated trigger (80 01 0f) writes panel config to FLEXI_CFG mass storage. Read while session is open, then logout. One-shot per session.
- Device probe: 52 02 28 for all devices (signal/battery for wireless), 94/96 diagnostics for bus devices (signal/voltage). Device type determined by rf_byte0 from config (0x10-0x1f = bus).

## Panel Facts (Development Unit)

- Model: JA-103K "JABLOTRON 100+", FW MD6112.07.0, HW MD15005
- 3 active sections, 28 peripherals, 7 users, 10 PG outputs
- 4-digit codes, CodesWithPrefix=false (prefix "999" = wildcard user lookup)
- ARCs enabled: 3 (real monitoring station - be careful with arm/disarm testing)

## Packet Types

| Type | Name              | Direction | Notes                                                             |
| ---- | ----------------- | --------- | ----------------------------------------------------------------- |
| 0x80 | UI_CONTROL        | Both      | Auth, modify section, PG, status/NAK                              |
| 0x52 | COMMAND           | Both      | Heartbeat, get-state, device status                               |
| 0x51 | SECTIONS_STATES   | IN        | 2 bytes/section (bits[7:6]=secondary + bits[5:0]=primary + flags) |
| 0x40 | SYS_INFO          | IN        | Model/hw/fw/regcode/mac/name                                      |
| 0x55 | DEVICE_STATE      | IN        | Individual device events                                          |
| 0xd8 | DEVICES_STATES    | IN        | Activity bitmap (little-endian)                                   |
| 0x30 | GET_SYS_INFO      | OUT       | Query: 30 01 <infotype>                                           |
| 0x90 | DEVICE_INFO       | IN        | Bus diagnostics response                                          |
| 0x94 | DIAGNOSTICS       | OUT       | Start/stop bus device diagnostics                                 |
| 0x96 | DIAGNOSTICS_CMD   | OUT       | Force info report from bus device                                 |
| 0x50 | PG_OUTPUTS_STATES | IN        | PG output states                                                  |

## Command Choreography (pcap-verified)

```
OUT  80 01 01                  AUTH_END (clear stale session)
OUT  80 08 03 39 39 39 xx xx   AUTH_CODE ("999" + PIN as ASCII)
IN   80 xx 0c ...              LOGIN_INFO (success) or 80 xx 1b 03 (wrong code)
OUT  80 02 0d <byte>           MODIFY_SECTION (0x9f+section=arm, 0x8f+section=disarm)
IN   80 xx 1a ...              COMMAND_ACK
OUT  80 01 01                  AUTH_END (logout)
```

Verified for section 2: arm=0xa1, disarm=0x91. Other sections use same formula (unverified).

## Code Encoding (pcap-verified)

Wire format: subtype 0x03 + (prefix + pin).encode('ascii')

- "999" prefix = wildcard user lookup (CodesWithPrefix=false)
- Kukulich's magic_offset=48 formula produces identical bytes (48+digit == ord(str(digit)))
- No separate "with prefix" encoding needed for our panel type

## Verified vs Unverified

Proven (pcap-verified + hardware-tested):

- TLV framing, heartbeat, enable device states, get sections, sysinfo queries
- Section state push on external change (instant)
- Section secondary state: bit 7 (0x80) of primary byte = ARMING (~30s exit delay observed)
- Section secondary state: bit 6 (0x40) of primary byte = PENDING (entry delay observed)
- Device activity bitmap (0xd8, little-endian)
- Code encoding: "999" + 4-digit code (ASCII) - pcap-verified
- MODIFY_SECTION section 2: arm=0xa1, disarm=0x91 - pcap-verified
- Login/logout choreography with timing (~7ms login, ~200ms ACK)
- Wrong code detection and handling
- Unauthenticated monitoring runs indefinitely
- Full arm/disarm from HA UI works end-to-end
- Config export via FLEXI_CFG (80 01 0f trigger, block device read)
- Device status query (52 02 28) - all devices, signal/battery for wireless
- Bus diagnostics (94/96) - signal quality, siren voltage, smoke battery
- Device names and section names from config binary (verified 32 devices)

Needs service window verification:

- MODIFY bytes for sections 1 and 3 (formula: 0x9f+N arm, 0x8f+N disarm)
- ARM_HOME/ARM_NIGHT command bytes (formula: 0xaf+section)
- 0x51 flag byte for TRIGGERED states
- Signal strength formula for 0xa8 responses (values change but % mapping unconfirmed)
- GET_DEVICE_STATUS (0x0a) unauthenticated access

## Ruff Config

`select = ["ALL"]` with minimal ignores. Python 3.14 target. No `from __future__ import annotations` needed. Tests exempt from D1xx/ANN201/S101/PLR2004.
