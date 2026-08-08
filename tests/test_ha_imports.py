"""Smoke-test the integration against the supported Home Assistant runtime."""

from __future__ import annotations

import importlib

import pytest


@pytest.mark.parametrize(
    "module",
    [
        "custom_components.amaran_ble",
        "custom_components.amaran_ble.config_flow",
        "custom_components.amaran_ble.device",
        "custom_components.amaran_ble.light",
        "custom_components.amaran_ble.number",
        "custom_components.amaran_ble.pending",
        "custom_components.amaran_ble.profiles",
        "custom_components.amaran_ble.reconfiguration",
        "custom_components.amaran_ble.repairs",
        "custom_components.amaran_ble.resolver",
        "custom_components.amaran_ble.select",
        "custom_components.amaran_ble.sensor",
        "custom_components.amaran_ble.switch",
    ],
)
def test_home_assistant_module_imports(module: str) -> None:
    """Every Home Assistant-facing module imports on the minimum Core version."""
    importlib.import_module(module)
