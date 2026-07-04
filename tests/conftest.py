"""
Shared pytest fixtures for the Jablotron Local integration tests.

Uses ``pytest-homeassistant-custom-component`` which provides the
``hass`` fixture, mock config-entry helpers and a full HA test bed.
"""

from collections.abc import Generator
from unittest.mock import patch

import pytest

from custom_components.jablotron_local.hidraw import DiscoveredPanel

SAMPLE_PANEL = DiscoveredPanel(
    path="/dev/hidraw3",
    serial="JA103K-0000001",
    name="JABLOTRON JA-100",
)


@pytest.fixture(autouse=True)
def _auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable loading of custom_components in every test."""


@pytest.fixture
def mock_enumerate_panels() -> Generator[list[DiscoveredPanel]]:
    """Patch :func:`hidraw.enumerate_panels` with a mutable list."""
    panels: list[DiscoveredPanel] = [SAMPLE_PANEL]
    with patch(
        "custom_components.jablotron_local.config_flow.enumerate_panels",
        return_value=panels,
    ):
        yield panels


@pytest.fixture
def mock_probe_device_ok() -> Generator[None]:
    """Patch :func:`hidraw.probe_device` to succeed silently."""
    with patch(
        "custom_components.jablotron_local.config_flow.probe_device",
        return_value=None,
    ):
        yield
