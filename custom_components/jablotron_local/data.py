"""
Runtime data types for the Jablotron Local integration.

Defines the typed ConfigEntry alias and the runtime data structure
stored in ``entry.runtime_data`` during the config entry lifecycle.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from .client import JablotronClient
    from .coordinator import JablotronCoordinator

type JablotronConfigEntry = ConfigEntry[JablotronData]


@dataclass
class JablotronData:
    """
    Runtime data for a Jablotron Local config entry.

    Created during entry setup and available via ``entry.runtime_data``.
    Cleaned up automatically on unload.
    """

    client: JablotronClient
    coordinator: JablotronCoordinator
