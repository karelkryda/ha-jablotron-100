"""
DataUpdateCoordinator for the Jablotron Local integration.

Combines push-based state updates with periodic polling:

- Section states and device activity are pushed by the panel via the
  USB HID reader thread in :mod:`client`. The coordinator receives
  decoded packets from the client callback and updates :attr:`data`,
  notifying all subscribed entities.
- Device status (battery, signal, voltage) is refreshed periodically
  (configurable, default 30 min) via :meth:`_async_update_data`, which
  re-probes all devices through the client's :meth:`probe_all_devices`.
  Results are stored in ``data.device_infos`` (keyed by device number).

The push callback is invoked from the reader thread; it uses
``hass.loop.call_soon_threadsafe`` to safely dispatch into HA's event
loop. The periodic probe only runs when a service PIN is configured.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .client import JablotronAuthError, JablotronCommandError
from .const import LOGGER
from .protocol import (
    CODE_PREFIX_WILDCARD,
    DeviceInfo,
    Packet,
    PacketType,
    SectionState,
    UiControl,
    UiStatusReason,
    decode_devices_states,
    decode_sections,
    decode_system_info,
    decode_ui_status,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .client import JablotronClient
    from .config_reader import PanelConfig


@dataclass
class PanelState:
    """
    Aggregated panel state maintained by the coordinator.

    Updated incrementally as packets arrive from the panel.
    """

    sections: list[SectionState] = field(default_factory=list)
    active_devices: frozenset[int] = field(default_factory=frozenset)
    system_info: dict[str, str] = field(default_factory=dict)
    device_infos: dict[int, DeviceInfo] = field(default_factory=dict)
    connected: bool = False


class JablotronCoordinator(DataUpdateCoordinator[PanelState]):
    """
    Coordinator for Jablotron panel state.

    Combines push-based section/device-activity updates from the panel
    with periodic device status probing (configurable interval when service
    PIN is configured). Bridges the reader thread into HA's event loop
    and notifies entities on every state change.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        client: JablotronClient,
        panel_config: PanelConfig | None = None,
        service_pin: str | None = None,
        probe_interval: int = 30,
    ) -> None:
        """
        Initialize the coordinator and wire the client callbacks.

        Args:
            hass: Home Assistant instance.
            client: Connected :class:`JablotronClient` instance.
            panel_config: Parsed panel configuration from FLEXI_CFG,
                or ``None`` if not available.
            service_pin: Service PIN for periodic device probing,
                or ``None`` to disable periodic probes.
            probe_interval: Interval in minutes between device probes.

        """
        super().__init__(
            hass,
            LOGGER,
            name="Jablotron Local",
            update_interval=timedelta(minutes=probe_interval) if service_pin else None,
        )
        self.client = client
        self.data = PanelState()
        self.panel_config = panel_config
        self._service_pin = service_pin

        # Wire callbacks - these fire from the reader thread.
        client.on_packets = self._on_packets_from_thread
        client.on_connection_change = self._on_connection_change_from_thread

        # Event signalled when initial sysinfo + sections arrive.
        self._initial_data_ready = asyncio.Event()

    def get_section_name(self, section_number: int) -> str | None:
        """
        Get the configured name for a section.

        Args:
            section_number: Section number (1-based).

        Returns:
            The section name from panel config, or ``None`` if not available.

        """
        if self.panel_config is None:
            return None

        return self.panel_config.section_names.get(section_number)

    async def _async_update_data(self) -> PanelState:
        """
        Periodic update: probe device status if service PIN is configured.

        Called by HA at the configured update_interval.
        Also called on first subscriber registration.
        """
        if (
            self._service_pin
            and self.panel_config
            and self.panel_config.devices
            and not self.client.service_pin_rejected
        ):
            statuses = await self.hass.async_add_executor_job(self._probe_devices)
            if statuses:
                self.data.device_infos = {s.device_number: s for s in statuses}

        return self.data

    def _probe_devices(self) -> list[DeviceInfo]:
        """Run the device probe on the executor thread."""
        if not self._service_pin or not self.panel_config:
            return []

        try:
            code = CODE_PREFIX_WILDCARD + self._service_pin
            return self.client.probe_all_devices(code, self.panel_config.devices)
        except JablotronAuthError, JablotronCommandError, OSError:
            LOGGER.warning("Periodic device probe failed", exc_info=True)
            return []

    async def async_wait_for_initial_data(self) -> None:
        """
        Wait until system info and section data have arrived.

        Called during entry setup to ensure device registration has
        model/firmware info and entities have initial section states.
        The panel typically responds within ~50ms; the 2s timeout is a
        generous upper bound.
        """
        try:
            async with asyncio.timeout(2.0):
                await self._initial_data_ready.wait()
        except TimeoutError:
            LOGGER.debug(
                "Initial data wait timed out (sections=%d, sysinfo=%s)",
                len(self.data.sections),
                list(self.data.system_info.keys()),
            )

    def _on_packets_from_thread(self, packets: list[Packet]) -> None:
        """
        Handle decoded packets from the reader thread.

        Dispatches to the HA event loop via ``call_soon_threadsafe``.
        """
        self.hass.loop.call_soon_threadsafe(self._process_packets, packets)

    def _on_connection_change_from_thread(self, connected: bool) -> None:  # noqa: FBT001
        """Handle connection state changes from the reader thread."""
        self.hass.loop.call_soon_threadsafe(self._process_connection_change, connected)

    def _process_packets(self, packets: list[Packet]) -> None:
        """
        Process packets on the HA event loop and notify entities.

        Called via ``call_soon_threadsafe`` from the reader thread callback.
        """
        changed = False

        for packet in packets:
            if packet.type == PacketType.SECTIONS:
                sections = decode_sections(packet.data)
                if sections != self.data.sections:
                    self.data.sections = sections
                    changed = True
                    LOGGER.debug(
                        "Sections updated: %s",
                        [(s.number, s.primary.name) for s in sections],
                    )

            elif packet.type == PacketType.DEVICES_STATES:
                activity = decode_devices_states(packet.data)
                if activity.active != self.data.active_devices:
                    self.data.active_devices = activity.active
                    changed = True
                    LOGGER.debug("Active devices: %s", sorted(activity.active))

            elif packet.type == PacketType.SYS_INFO:
                info = decode_system_info(packet.data)
                if info is not None:
                    self.data.system_info[info.kind.name.lower()] = info.value
                    LOGGER.debug("System info %s = %s", info.kind.name, info.value)

            elif packet.type == PacketType.UI_CONTROL:
                self._handle_ui_control(packet)

            else:
                LOGGER.debug(
                    "Unhandled packet type=0x%02x data=%s",
                    packet.type,
                    packet.data.hex() if packet.data else "",
                )

        if changed:
            self.async_update_listeners()

        # Signal initial data ready once we have sections + model.
        if (
            not self._initial_data_ready.is_set()
            and self.data.sections
            and self.data.system_info.get("model")
        ):
            self._initial_data_ready.set()

    def _handle_ui_control(self, packet: Packet) -> None:
        """Dispatch UI_CONTROL subtypes (status/NAK detection)."""
        if not packet.data:
            return

        subtype = packet.data[0]
        if subtype == UiControl.STATUS:
            status = decode_ui_status(packet.data)
            if (
                status is not None
                and status.reason == UiStatusReason.WRONG_CODE
                and not self.client.user_initiated_action
            ):
                # WRONG_CODE without user action means the configured
                # service PIN is invalid or the integration sent a PIN
                # it shouldn't have. Stop and force reauth.
                LOGGER.error(
                    "Panel reported WRONG_CODE without user action; "
                    "forcing reauthentication"
                )
                self.client.service_pin_rejected = True
                if self.config_entry:
                    self.config_entry.async_start_reauth(self.hass)

    def _process_connection_change(self, connected: bool) -> None:  # noqa: FBT001
        """Update connection state and notify entities."""
        if self.data.connected == connected:
            return

        self.data.connected = connected
        if connected:
            LOGGER.info("Panel connection established")
        else:
            LOGGER.warning("Panel connection lost; reconnecting")

        self.async_update_listeners()
