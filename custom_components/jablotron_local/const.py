"""
Constants for the Jablotron Local integration.

Integration-wide identifiers shared across config_flow, __init__,
coordinator, and entity modules. Protocol-specific constants (packet
types, timing, enums) live in ``protocol.py`` to keep the codec
self-contained and independently testable.
"""

from logging import Logger, getLogger

LOGGER: Logger = getLogger(__package__)

DOMAIN = "jablotron_local"

# Jablotron JA-100+ panel USB HID identifiers
USB_VENDOR_ID = 0x16D6
USB_PRODUCT_ID = 0x0008
