"""
Push-style DataUpdateCoordinator for the Jablotron Local integration.

Unlike a polling coordinator, this one has no ``update_interval``. The
panel pushes section states, device activity, and device events via the
USB HID reader thread in :mod:`client`. The coordinator receives decoded
packets from the client callback and updates its :attr:`data` dict,
notifying all subscribed entities.

The callback is invoked from the reader thread; it uses
``hass.loop.call_soon_threadsafe`` to safely dispatch into HA's event
loop.
"""

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import LOGGER
from .protocol import (
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
    connected: bool = False


class JablotronCoordinator(DataUpdateCoordinator[PanelState]):
    """
    Push-style coordinator for Jablotron panel state.

    No polling - the panel pushes data via the USB HID reader thread.
    The coordinator bridges the reader thread into HA's event loop and
    notifies entities on every state change.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        client: JablotronClient,
        panel_config: PanelConfig | None = None,
    ) -> None:
        """
        Initialize the coordinator and wire the client callbacks.

        Args:
            hass: Home Assistant instance.
            client: Connected :class:`JablotronClient` instance.
            panel_config: Parsed panel configuration from FLEXI_LOG,
                or ``None`` if not available.

        """
        super().__init__(
            hass,
            LOGGER,
            name="Jablotron Local",
        )
        self.client = client
        self.data = PanelState()
        self.panel_config = panel_config

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
        Return the current panel state (no polling).

        Called by HA on first subscriber registration. Since we're
        push-based, just return whatever we have.
        """
        return self.data

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

        if changed:
            self.async_set_updated_data(self.data)

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

        self.async_set_updated_data(self.data)
