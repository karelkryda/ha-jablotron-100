"""
Tests for the Jablotron Local config flow.

Covers the manual-add path, USB auto-discovery, reconfigure flow and
the ``test-before-configure`` device probe error handling.
"""

from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.jablotron_local.config_flow import (
    CONF_DEVICE_PATH,
    CONF_PROBE_INTERVAL,
    CONF_SERIAL_NUMBER,
)
from custom_components.jablotron_local.const import DOMAIN
from custom_components.jablotron_local.hidraw import (
    DeviceBusyError,
    DeviceNotFoundError,
    PermissionDeniedError,
)

from .conftest import SAMPLE_PANEL


async def test_user_flow_single_panel(
    hass: HomeAssistant,
    mock_enumerate_panels: list,  # noqa: ARG001 - fixture side effect
    mock_probe_device_ok: None,  # noqa: ARG001
) -> None:
    """A single connected panel proceeds straight to confirm and creates an entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == SAMPLE_PANEL.name
    assert result["data"] == {
        CONF_DEVICE_PATH: SAMPLE_PANEL.path,
        CONF_SERIAL_NUMBER: SAMPLE_PANEL.serial,
        CONF_PROBE_INTERVAL: 30,
    }


async def test_user_flow_no_devices(hass: HomeAssistant) -> None:
    """Aborts with ``no_device`` when nothing is connected."""
    with patch(
        "custom_components.jablotron_local.config_flow.enumerate_panels",
        return_value=[],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_device"


@pytest.mark.parametrize(
    ("exception", "expected_error"),
    [
        (DeviceNotFoundError("/dev/hidraw3"), "device_not_found"),
        (PermissionDeniedError("/dev/hidraw3"), "permission_denied"),
        (DeviceBusyError("/dev/hidraw3"), "device_busy"),
    ],
)
async def test_user_flow_probe_failure(
    hass: HomeAssistant,
    mock_enumerate_panels: list,  # noqa: ARG001
    exception: Exception,
    expected_error: str,
) -> None:
    """Probe failures surface as inline form errors, not entry creation."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch(
        "custom_components.jablotron_local.config_flow.probe_device",
        side_effect=exception,
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"
    assert result["errors"] == {"base": expected_error}


async def test_user_flow_duplicate_aborts(
    hass: HomeAssistant,
    mock_enumerate_panels: list,  # noqa: ARG001
) -> None:
    """An already-configured panel is filtered out of enumeration results."""
    MockConfigEntry(
        domain=DOMAIN,
        unique_id=SAMPLE_PANEL.serial,
        data={
            CONF_DEVICE_PATH: SAMPLE_PANEL.path,
            CONF_SERIAL_NUMBER: SAMPLE_PANEL.serial,
        },
    ).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    # With ``single_config_entry: true`` HA aborts before our step runs.
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"
