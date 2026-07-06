"""
Config flow for the Jablotron Local integration.

Entry points:

- ``async_step_usb``: triggered automatically when a panel matching the
  manifest USB VID/PID is plugged in. Existing entries have their
  ``device_path`` refreshed if the kernel assigned a new ``/dev/hidraw*``.
- ``async_step_user``: manual "Add integration" flow. Enumerates
  currently connected panels via sysfs.
- ``async_step_reconfigure``: allows the user to point an existing entry
  at a different panel (e.g. after replacing hardware).

The final ``async_step_confirm`` step performs a "test-before-configure"
probe by opening the ``/dev/hidraw*`` character device to verify
permissions and exclusive access before persisting the entry.
"""

from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigFlow,
    ConfigFlowResult,
)

from .const import DOMAIN, LOGGER
from .hidraw import (
    DeviceBusyError,
    DeviceNotFoundError,
    DeviceOpenError,
    DiscoveredPanel,
    PermissionDeniedError,
    enumerate_panels,
    probe_device,
)

if TYPE_CHECKING:
    from homeassistant.helpers.service_info.usb import UsbServiceInfo

CONF_DEVICE_PATH = "device_path"
CONF_SERIAL_NUMBER = "serial_number"
CONF_SERVICE_PIN = "service_pin"
CONF_PROBE_INTERVAL = "probe_interval"
DEFAULT_PROBE_INTERVAL = 30  # minutes


class JablotronLocalConfigFlow(ConfigFlow, domain=DOMAIN):
    """Config flow for Jablotron Local."""

    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovered_panel: DiscoveredPanel | None = None

    async def async_step_reauth(self, _entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Handle reauthentication triggered by invalid service PIN."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Prompt user to re-enter service PIN."""
        errors: dict[str, str] = {}
        if user_input is not None:
            service_pin = user_input.get(CONF_SERVICE_PIN, "").strip()
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(),
                data_updates={
                    CONF_SERVICE_PIN: service_pin or None,
                },
            )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_SERVICE_PIN, default=""): str,
                }
            ),
            errors=errors,
        )

    async def async_step_usb(self, discovery_info: UsbServiceInfo) -> ConfigFlowResult:
        """
        Handle a panel discovered by Home Assistant's USB integration.

        HA fires this when a device matching the manifest ``usb`` matcher
        is plugged in. If we already have a config entry for this panel
        (matched by serial), we update its stored ``device_path`` in case
        the kernel assigned a different ``/dev/hidraw*`` this time -
        satisfying the ``discovery-update-info`` quality rule.
        """
        serial = discovery_info.serial_number or ""
        if not serial:
            # A panel without a serial cannot be uniquely tracked across
            # replugs; fall back to the device path as unique id but log
            # loudly since this typically indicates a kernel quirk.
            LOGGER.warning(
                "Discovered Jablotron panel at %s has no serial number; "
                "using device path as unique id",
                discovery_info.device,
            )

        unique_id = serial or discovery_info.device
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured(
            updates={CONF_DEVICE_PATH: discovery_info.device}
        )

        name = _describe(
            discovery_info.description,
            discovery_info.manufacturer,
        )

        self._discovered_panel = DiscoveredPanel(
            path=discovery_info.device,
            serial=serial,
            name=name,
        )
        self.context["title_placeholders"] = {"name": name}

        return await self.async_step_confirm()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """
        Handle the manual "Add integration" step.

        Enumerates connected panels and filters out any already
        configured. Depending on how many remain the flow either picks
        one automatically, shows a picker, or aborts.
        """
        panels = await self.hass.async_add_executor_job(enumerate_panels)

        configured_serials = {
            entry.unique_id
            for entry in self._async_current_entries()
            if entry.unique_id
        }
        available = [
            panel
            for panel in panels
            if not panel.serial or panel.serial not in configured_serials
        ]

        if not available:
            return self.async_abort(reason="no_device")

        if user_input is not None:
            selected = next(
                (p for p in available if p.path == user_input[CONF_DEVICE_PATH]),
                None,
            )
            if selected is None:
                return self.async_abort(reason="no_device")
            self._discovered_panel = selected
        elif len(available) == 1:
            self._discovered_panel = available[0]
        else:
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_DEVICE_PATH): vol.In(
                            {panel.path: _panel_label(panel) for panel in available}
                        )
                    }
                ),
            )

        await self.async_set_unique_id(
            self._discovered_panel.serial or self._discovered_panel.path
        )
        self._abort_if_unique_id_configured()

        return await self.async_step_confirm()

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """
        Handle the reconfigure flow.

        Lets the user point an existing entry at a different connected
        panel - for example after replacing the panel or a USB
        reassignment that renamed ``/dev/hidraw*`` before HA had a chance
        to auto-update via discovery.
        """
        entry = self._get_reconfigure_entry()
        panels = await self.hass.async_add_executor_job(enumerate_panels)

        if not panels:
            return self.async_abort(reason="no_device")

        if user_input is not None:
            selected = next(
                (p for p in panels if p.path == user_input[CONF_DEVICE_PATH]),
                None,
            )
            if selected is None:
                return self.async_abort(reason="no_device")

            await self.async_set_unique_id(selected.serial or selected.path)
            self._abort_if_unique_id_mismatch()

            return self.async_update_reload_and_abort(
                entry,
                data_updates={
                    CONF_DEVICE_PATH: selected.path,
                    CONF_SERIAL_NUMBER: selected.serial,
                    CONF_SERVICE_PIN: user_input.get(CONF_SERVICE_PIN, "").strip()
                    or None,
                    CONF_PROBE_INTERVAL: user_input.get(
                        CONF_PROBE_INTERVAL, DEFAULT_PROBE_INTERVAL
                    ),
                },
            )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_DEVICE_PATH,
                        default=entry.data.get(CONF_DEVICE_PATH),
                    ): vol.In({panel.path: _panel_label(panel) for panel in panels}),
                    vol.Optional(
                        CONF_SERVICE_PIN,
                        default=entry.data.get(CONF_SERVICE_PIN, ""),
                    ): str,
                    vol.Optional(
                        CONF_PROBE_INTERVAL,
                        default=entry.data.get(
                            CONF_PROBE_INTERVAL, DEFAULT_PROBE_INTERVAL
                        ),
                    ): int,
                }
            ),
        )

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """
        Confirm setup and probe the device.

        Shows the panel description to the user. On submit, opens the
        hidraw character device with ``O_RDWR`` to verify permissions
        and exclusivity (the ``test-before-configure`` quality rule).
        On success, creates the config entry.
        """
        panel = self._discovered_panel
        if panel is None:
            return self.async_abort(reason="no_device")

        errors: dict[str, str] = {}
        if user_input is not None:
            error = await self._async_probe(panel.path)
            if error is None:
                data: dict[str, Any] = {
                    CONF_DEVICE_PATH: panel.path,
                    CONF_SERIAL_NUMBER: panel.serial,
                }
                service_pin = user_input.get(CONF_SERVICE_PIN, "").strip()
                if service_pin:
                    data[CONF_SERVICE_PIN] = service_pin

                probe_interval = user_input.get(
                    CONF_PROBE_INTERVAL, DEFAULT_PROBE_INTERVAL
                )
                data[CONF_PROBE_INTERVAL] = probe_interval

                return self.async_create_entry(
                    title=panel.name,
                    data=data,
                )

            errors["base"] = error

        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_SERVICE_PIN, default=""): str,
                    vol.Optional(
                        CONF_PROBE_INTERVAL, default=DEFAULT_PROBE_INTERVAL
                    ): int,
                }
            ),
            description_placeholders={
                "name": panel.name,
                "path": panel.path,
                "serial": panel.serial or "-",
            },
            errors=errors,
        )

    async def _async_probe(self, path: str) -> str | None:
        """
        Probe the hidraw device for accessibility.

        Runs :func:`probe_device` in the executor and maps its exceptions
        to translation keys under ``config.error`` in ``translations``.

        Args:
            path: Character device path to probe.

        Returns:
            A translation key on failure, ``None`` on success.

        """
        try:
            await self.hass.async_add_executor_job(probe_device, path)
        except DeviceNotFoundError:
            LOGGER.warning("Panel disappeared before setup: %s", path)
            return "device_not_found"
        except PermissionDeniedError:
            LOGGER.warning("No permission to access panel at %s", path)
            return "permission_denied"
        except DeviceBusyError:
            LOGGER.warning("Panel at %s is held by another process", path)
            return "device_busy"
        except DeviceOpenError:
            LOGGER.exception("Unexpected error probing panel at %s", path)
            return "cannot_connect"

        return None

    @property
    def _is_reconfigure(self) -> bool:
        """Return True if the flow was started from an existing entry."""
        return self.source == SOURCE_RECONFIGURE


def _describe(description: str | None, manufacturer: str | None) -> str:
    """
    Compose a human-readable name from USB descriptor fields.

    Falls back through description → manufacturer → generic label.
    """
    if description:
        return description

    if manufacturer:
        return f"{manufacturer} panel"

    return "Jablotron Panel"


def _panel_label(panel: DiscoveredPanel) -> str:
    """Format a panel entry for a selection dropdown."""
    if panel.serial:
        return f"{panel.name} ({panel.serial}) - {panel.path}"

    return f"{panel.name} - {panel.path}"
