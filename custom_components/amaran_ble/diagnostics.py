"""Diagnostics support for amaran BLE."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.core import HomeAssistant

from . import AmaranConfigEntry
from .const import (
    CONF_APP_KEY,
    CONF_DEVICE_KEY,
    CONF_INITIAL_SEQUENCE,
    CONF_IV_INDEX,
    CONF_LOCAL_ADDRESS,
    CONF_NET_KEY,
    CONF_SEQUENCE_STORE_ID,
    CONF_UNICAST_ADDRESS,
)

_CONFIG_ENTRY_KEYS_TO_REDACT = frozenset(
    {
        CONF_ADDRESS,
        CONF_NAME,
        CONF_NET_KEY,
        CONF_APP_KEY,
        CONF_DEVICE_KEY,
        CONF_SEQUENCE_STORE_ID,
        CONF_LOCAL_ADDRESS,
        CONF_UNICAST_ADDRESS,
        CONF_IV_INDEX,
        CONF_INITIAL_SEQUENCE,
        # The entry unique ID is the fixture's Bluetooth address. The entry ID
        # was also the sequence-store ID in older versions of the integration.
        "unique_id",
        "entry_id",
        "title",
        # Bluetooth discovery keys can embed the fixture address in their keys.
        "discovery_keys",
    }
)


def _diagnostic_value(value: Any) -> Any:
    """Convert protocol dataclasses into deterministic JSON-safe values."""
    if isinstance(value, Enum):
        return _diagnostic_value(value.value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        # No current decoded state contains bytes. Redacting them defensively
        # prevents a future protocol state from accidentally exposing key data.
        return REDACTED
    if is_dataclass(value) and not isinstance(value, type):
        return {
            state_field.name: _diagnostic_value(getattr(value, state_field.name))
            for state_field in fields(value)
        }
    if isinstance(value, Mapping):
        return {
            str(key): _diagnostic_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_diagnostic_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_diagnostic_value(item) for item in value), key=repr)
    # Keep diagnostics serializable if a future state contains a helper object,
    # without including a repr that could contain an address or memory location.
    return f"<{type(value).__name__}>"


def _decoded_state(value: Any) -> dict[str, Any] | None:
    """Return one typed, JSON-safe decoded protocol state."""
    if value is None:
        return None
    state = _diagnostic_value(value)
    return {
        "type": type(value).__name__,
        "data": state,
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: AmaranConfigEntry
) -> dict[str, Any]:
    """Return redacted config, catalog capabilities, and decoded state."""
    device = entry.runtime_data

    return {
        "config_entry": async_redact_data(
            entry.as_dict(), _CONFIG_ENTRY_KEYS_TO_REDACT
        ),
        "profile": _diagnostic_value(device.profile),
        "runtime": {
            "connected": device.connected,
            "available": device.available,
            "preferred_green_magenta": device.preferred_gm,
            "available_fan_modes": list(device.available_fan_modes),
            "states": {
                "light": _decoded_state(device.state),
                "effect": _decoded_state(device.effect_state),
                "boost": _decoded_state(device.boost_state),
                "fan": _decoded_state(device.fan_state),
                "power": _decoded_state(device.power_state),
                "version": _decoded_state(device.version_state),
                "advanced_capabilities": _decoded_state(device.version2_state),
                "high_speed_photography": _decoded_state(device.high_speed_state),
            },
        },
    }
