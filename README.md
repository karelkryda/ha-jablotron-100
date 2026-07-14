# Jablotron Local

[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)

Local control of Jablotron JA-100+ alarm panels via USB HID - no cloud, no Jablonet dependency.

> [!IMPORTANT]
> This is a personal project built for my own Jablotron panel. I'm sharing it publicly in case it helps someone else, but please keep in mind that I can't guarantee it will work with your setup. I won't necessarily respond to issues or feature requests, and I only plan to extend the integration as far as my own needs go. If it works for you - awesome! If not - feel free to fork it and make it your own.

## Features

- Direct local communication over USB HID (panel's JA-Link/F-Link port)
- Unauthenticated push-based state monitoring (sections, devices)
- Per-user PIN attribution for arm/disarm commands
- Alarm control panel entities per active section (with real section names)
- Exit delay (arming) state shown during arm countdown
- Entry delay (pending) state shown when alarm is about to trigger
- Triggered state shown when alarm is firing
- Binary sensor entities for all devices (motion, door, smoke, flood, etc.)
- Battery and signal strength sensors for wireless devices
- Battery voltage sensors for sirens
- Bus signal quality sensors for wired devices
- Device and section names read from panel configuration (FLEXI_CFG)
- Configurable periodic device status refresh (default 30 min)
- Reauth flow on invalid service PIN

## Requirements

- Jablotron JA-100+ panel (JA-101K, JA-103K, JA-106K, etc.)
- USB connection from the panel to the Home Assistant host
- JA-Link / F-Link software must be closed (exclusive HID access)

## Installation

### HACS (recommended)

1. Add this repository as a custom repository in HACS
2. Install "Jablotron Local"
3. Restart Home Assistant

### Manual

Copy `custom_components/jablotron_local` to your HA `config/custom_components/` directory.

## Configuration

1. Connect the panel's USB cable to your Home Assistant host
2. Close JA-Link / F-Link if running
3. Go to Settings > Devices & Services > Add Integration
4. Search for "Jablotron Local"
5. The integration will auto-detect the panel on USB
6. Optionally enter your service/installer PIN for device names and status probing
7. Optionally adjust the probe interval (default 30 minutes)

### Stable device path (Docker / USB/IP)

The integration automatically prefers stable `/dev` symlinks over raw `/dev/hidrawN` paths. If you use udev rules to create symlinks, the integration will discover and use them - surviving kernel device number reassignments across reboots or USB/IP reattachments.

Example udev rule (`/etc/udev/rules.d/99-jablotron.rules`):

```
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="16d6", ATTRS{idProduct}=="0008", SYMLINK+="jablotron-hid", MODE="0666"
```

For Docker deployments, mount `/dev` into the container and ensure the cgroup rules permit access to the hidraw device major number.

## Usage

Once configured, the integration creates:

- **Alarm control panel** entities per active section (arm/disarm via PIN)
- **Binary sensor** entities per device (on/off from activity bitmap)
- **Sensor** entities for battery, signal strength, and voltage (if service PIN provided)

To arm or disarm from Home Assistant, enter your 4-digit panel PIN in the keypad. The integration automatically prepends the "999" wildcard prefix so the panel identifies you from your PIN.

### Service PIN

Providing the service/installer PIN enables:

- Reading device and section names from the panel configuration
- Probing device status (battery level, signal strength, siren voltage)
- Periodic refresh of device status (configurable interval)

Without the service PIN, the integration still works for monitoring and arm/disarm, but uses generic names and no battery/signal sensors.

## How it works

The integration communicates with the panel over the same USB HID interface used by JA-Link/F-Link. State monitoring is completely unauthenticated and code-free - it cannot trip the alarm. Arm/disarm commands authenticate with the user's own PIN per action (never stored, never repeated on a timer).

Device names and status are read from the panel's FLEXI_CFG mass storage volume and via authenticated device probe commands, using the service/installer PIN. This happens at startup and periodically thereafter.

## Credits

Built on USB protocol reverse-engineering from JA-Link captures and kukulich's home-assistant-jablotron100 as a protocol reference.
