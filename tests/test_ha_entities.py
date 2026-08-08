"""Home Assistant entity behavior for configured fixture capabilities."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("homeassistant")

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ColorMode,
)
from homeassistant.const import CONF_ADDRESS

from custom_components.amaran_ble import light, number
from custom_components.amaran_ble.amaranble.telink import LightState
from custom_components.amaran_ble.const import (
    CONF_MAX_KELVIN,
    CONF_MIN_KELVIN,
    CONF_SUPPORTS_CCT,
    CONF_SUPPORTS_COLOR,
)


def make_entry(options: dict, *, state: LightState | None = None) -> SimpleNamespace:
    """Build the small config-entry surface used by entity constructors."""
    runtime_data = SimpleNamespace(
        available=state is not None,
        state=state,
        preferred_gm=0,
        async_apply_turn_on=AsyncMock(),
        async_set_gm=AsyncMock(),
    )
    return SimpleNamespace(
        runtime_data=runtime_data,
        data={CONF_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        options=options,
        title="Test light",
    )


@pytest.mark.parametrize(
    ("options", "expected_modes"),
    [
        (
            {CONF_SUPPORTS_CCT: False, CONF_SUPPORTS_COLOR: False},
            {ColorMode.BRIGHTNESS},
        ),
        (
            {CONF_SUPPORTS_CCT: True, CONF_SUPPORTS_COLOR: False},
            {ColorMode.COLOR_TEMP},
        ),
        (
            {CONF_SUPPORTS_CCT: True, CONF_SUPPORTS_COLOR: True},
            {ColorMode.COLOR_TEMP, ColorMode.HS},
        ),
    ],
)
def test_light_entity_advertises_only_configured_capabilities(
    options: dict, expected_modes: set[ColorMode]
) -> None:
    """Brightness, bi-colour, and full-colour profiles expose valid HA modes."""
    entity = light.AmaranLightEntity(make_entry(options))

    assert entity.supported_color_modes == expected_modes


@pytest.mark.asyncio
async def test_light_entity_clamps_cct_service_calls_to_configured_range() -> None:
    """Service data outside a fixture profile must be clamped before BLE send."""
    state = LightState(
        on=True,
        is_hsi=False,
        intensity=500,
        kelvin=4300,
        gm=0,
        hue=0,
        saturation=0,
    )
    entry = make_entry(
        {
            CONF_SUPPORTS_CCT: True,
            CONF_SUPPORTS_COLOR: False,
            CONF_MIN_KELVIN: 2700,
            CONF_MAX_KELVIN: 6500,
        },
        state=state,
    )
    entity = light.AmaranLightEntity(entry)

    await entity.async_turn_on(**{ATTR_BRIGHTNESS: 255, ATTR_COLOR_TEMP_KELVIN: 9000})

    entry.runtime_data.async_apply_turn_on.assert_awaited_once_with(
        intensity=1000,
        brightness_changed=True,
        kelvin=6500,
        hs_color=None,
    )


@pytest.mark.asyncio
async def test_gm_number_forwards_fractional_value_for_device_rounding() -> None:
    """The Number entity must not apply Python's ties-to-even rounding first."""
    entry = make_entry({})
    entity = number.AmaranGreenMagentaEntity(entry)

    await entity.async_set_native_value(2.5)

    entry.runtime_data.async_set_gm.assert_awaited_once_with(2.5)
