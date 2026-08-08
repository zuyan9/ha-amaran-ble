"""Smoke-test the integration against the supported Home Assistant runtime."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

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


@pytest.mark.parametrize(
    "path",
    [
        Path("custom_components/amaran_ble/strings.json"),
        Path("custom_components/amaran_ble/translations/en.json"),
    ],
)
def test_repair_issue_uses_exactly_one_description_branch(path: Path) -> None:
    """Fixable Repairs must use fix_flow instead of a parallel description."""
    issue = json.loads(path.read_text())["issues"]["factory_reset"]

    assert set(issue) & {"description", "fix_flow"} == {"fix_flow"}
