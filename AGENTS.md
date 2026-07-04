# AGENTS.md

## Project Overview

Home Assistant custom integration for local control of Jablotron JA-100+ alarm panels via USB HID. Bypasses Jablonet cloud entirely using the panel's J-Link/F-Link USB port with raw Linux hidraw I/O (no native dependencies).

## Architecture

- `protocol.py` - TLV framing codec. Builds/splits 64-byte HID reports, packet constants (IntEnum), encode/decode helpers, dataclasses for parsed data. Pure functions, zero I/O, zero HA imports. Fully unit-tested against pcap vectors.
- `hidraw.py` - Low-level device infrastructure. Enumerates panels via sysfs, probes device accessibility (permissions, exclusive access). Used by config_flow.
- `client.py` - Blocking USB HID client. Background reader thread runs code-free monitoring loop (heartbeat + enable device states). Pushes state changes to coordinator via callback. Command path (modify_section) authenticates per-action via threading events. Called via `async_add_executor_job`.
- `coordinator.py` - Push-style DataUpdateCoordinator (no polling interval). Receives updates from the reader thread via `hass.loop.call_soon_threadsafe`. Waits for initial sysinfo+sections before platform setup.
- `alarm_control_panel.py` - AlarmControlPanelEntity per active section. State from 0x51 push via `_attr_alarm_state`. Arm/disarm via per-user PIN (prefix "999" prepended automatically).
- `config_flow.py` - USB auto-discovery (manifest VID/PID matcher) + manual sysfs enumeration + reconfigure flow. Probes device before entry creation (test-before-configure).
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
- Push-style coordinator: no polling. Sections are pushed by the panel on change. Periodic reports serve as baseline resync.
- Raw /dev/hidraw over hidapi: zero native dependencies, Linux-only (HA OS target). O_NONBLOCK reads + sleep loop in reader thread.
- Reader thread owns all reads. Command path signals via threading.Event (no fd contention).
- Auto-reconnect: exponential backoff (1s-60s) on USB errors. Re-sends startup sequence on reconnect.
- kukulich's keepalive bug: he re-sends AUTH_CODE on every keepalive cycle. JA-Link never does this. We use 52 01 02 (COMMAND heartbeat) which works unauthenticated.

## Panel Facts (Development Unit)

- Model: JA-103K "JABLOTRON 100+", FW MD6112.07.0, HW MD15005
- 3 active sections, 28 peripherals, 7 users, 10 PG outputs
- 4-digit codes, CodesWithPrefix=false (prefix "999" = wildcard user lookup)
- ARCs enabled: 3 (real monitoring station - be careful with arm/disarm testing)

## Packet Types

| Type | Name              | Direction | Notes                                   |
| ---- | ----------------- | --------- | --------------------------------------- |
| 0x80 | UI_CONTROL        | Both      | Auth, modify section, PG, status/NAK    |
| 0x52 | COMMAND           | OUT       | Heartbeat, get-state, enable dev states |
| 0x51 | SECTIONS_STATES   | IN        | 2 bytes/section (primary + flags)       |
| 0x40 | SYS_INFO          | IN        | Model/hw/fw/regcode/mac/name            |
| 0x55 | DEVICE_STATE      | IN        | Individual device events                |
| 0xd8 | DEVICES_STATES    | IN        | Activity bitmap (little-endian)         |
| 0x30 | GET_SYS_INFO      | OUT       | Query: 30 01 <infotype>                 |
| 0x90 | DEVICE_INFO       | IN        | Device details                          |
| 0x50 | PG_OUTPUTS_STATES | IN        | PG output states                        |

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
- Device activity bitmap (0xd8, little-endian)
- Code encoding: "999" + 4-digit code (ASCII) - pcap-verified
- MODIFY_SECTION section 2: arm=0xa1, disarm=0x91 - pcap-verified
- Login/logout choreography with timing (~7ms login, ~200ms ACK)
- Wrong code detection and handling
- Unauthenticated monitoring runs indefinitely
- Full arm/disarm from HA UI works end-to-end

Needs service window verification:

- MODIFY bytes for sections 1 and 3 (formula: 0x9f+N arm, 0x8f+N disarm)
- ARM_HOME/ARM_NIGHT command bytes (formula: 0xaf+section, same as kukulich)
- 0x51 flag byte for PENDING/ARMING/TRIGGERED states
- 0x55 per-device parse (battery/signal/tamper)
- GET_DEVICE_STATUS (0x0a) unauthenticated access

## Ruff Config

`select = ["ALL"]` with minimal ignores. Python 3.14 target. No `from __future__ import annotations` needed. Tests exempt from D1xx/ANN201/S101/PLR2004.
