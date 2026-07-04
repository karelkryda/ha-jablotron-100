# Jablotron Local

[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)

Local control of Jablotron JA-100+ alarm panels via USB HID - no cloud, no Jablonet dependency.

## Features

- Direct local communication over USB HID (panel's J-Link/F-Link port)
- Unauthenticated push-based state monitoring (sections, devices)
- Per-user PIN attribution for arm/disarm commands
- Alarm control panel entities per active section
- Binary sensors for motion, door/window, smoke, flood, tamper (planned)
- Sensors for battery, signal strength (planned)

## Requirements

- Jablotron JA-100+ panel (JA-101K, JA-103K, JA-106K, etc.)
- USB connection from the panel to the Home Assistant host
- J-Link / F-Link software must be closed (exclusive HID access)

## Installation

### HACS (recommended)

1. Add this repository as a custom repository in HACS
2. Install "Jablotron Local"
3. Restart Home Assistant

### Manual

Copy `custom_components/jablotron_local` to your HA `config/custom_components/` directory.

## Configuration

1. Connect the panel's USB cable to your Home Assistant host
2. Close J-Link / F-Link if running
3. Go to Settings > Devices & Services > Add Integration
4. Search for "Jablotron Local"
5. The integration will auto-detect the panel on USB

## Usage

Once configured, the integration creates one alarm control panel entity per active section on your panel. Each entity shows the current state (disarmed, armed away, armed home) and provides arm/disarm controls.

To arm or disarm from Home Assistant, enter your 4-digit panel PIN in the keypad. The integration automatically prepends the "999" wildcard prefix so the panel identifies you from your PIN.

## How it works

The integration communicates with the panel over the same USB HID interface used by JA-Link/F-Link. State monitoring is completely unauthenticated and code-free - it cannot trip the alarm. Arm/disarm commands authenticate with the user's own PIN per action (never stored, never repeated on a timer).

## Credits

Built on USB protocol reverse-engineering from JA-Link captures and kukulich's home-assistant-jablotron100 as a protocol reference.
