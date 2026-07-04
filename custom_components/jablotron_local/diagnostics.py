"""
Diagnostics support for the Jablotron Local integration.

Exposes a JSON blob via Home Assistant's diagnostics platform. Users
can download this from *Settings → Devices & Services → Jablotron
Local → Download diagnostics* and attach it to bug reports without
leaking sensitive data.
"""

from typing import TYPE_CHECKING, Any

from .config_flow import CONF_DEVICE_PATH, CONF_SERIAL_NUMBER

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.core import HomeAssistant

    from .data import JablotronConfigEntry

# Fields that must be redacted from diagnostics output. The serial
# number is device-identifying but not sensitive; we still redact it to
# keep public issue trackers clean.
_REDACT_KEYS: frozenset[str] = frozenset({CONF_SERIAL_NUMBER})


async def async_get_config_entry_diagnostics(
    _hass: HomeAssistant, entry: JablotronConfigEntry
) -> dict[str, Any]:
    """
    Return diagnostics for a Jablotron Local config entry.

    Includes the entry metadata (with sensitive fields redacted) and
    static integration information.
    """
    return {
        "entry": {
            "title": entry.title,
            "version": entry.version,
            "minor_version": entry.minor_version,
            "source": entry.source,
            "unique_id_present": entry.unique_id is not None,
            "data": _redact(entry.data),
            "options": _redact(entry.options),
        },
        "device": {
            "path": entry.data.get(CONF_DEVICE_PATH),
        },
    }


def _redact(mapping: Mapping[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of ``mapping`` with sensitive values masked."""
    return {
        key: ("**REDACTED**" if key in _REDACT_KEYS and value else value)
        for key, value in mapping.items()
    }
