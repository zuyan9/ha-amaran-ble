"""Home Assistant entity behavior for configured fixture capabilities."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

pytest.importorskip("homeassistant")

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_EFFECT,
    ColorMode,
    LightEntityFeature,
)
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import (
    CONF_ADDRESS,
    PERCENTAGE,
    REVOLUTIONS_PER_MINUTE,
    EntityCategory,
    UnitOfTemperature,
    UnitOfTime,
)

from custom_components.amaran_ble import light, number, select, sensor, switch
from custom_components.amaran_ble.amaranble.telink import (
    EffectState,
    LightState,
    SystemEffect,
)
from custom_components.amaran_ble.const import (
    CONF_MAX_KELVIN,
    CONF_MIN_KELVIN,
    CONF_MODEL,
    CONF_SUPPORTS_CCT,
    CONF_SUPPORTS_COLOR,
    PROFILE_ACE_25X,
    PROFILE_GENERIC,
)
from custom_components.amaran_ble.profiles import get_fixture_profile


def make_entry(
    options: dict,
    *,
    state: LightState | None = None,
    available: bool | None = None,
    **runtime_values: object,
) -> SimpleNamespace:
    """Build the small config-entry surface used by entity constructors."""
    runtime_data = SimpleNamespace(
        available=state is not None if available is None else available,
        connected=state is not None if available is None else available,
        state=state,
        profile=get_fixture_profile(options.get(CONF_MODEL)),
        preferred_gm=0,
        effect_state=None,
        effect_frequency_available=False,
        effect_color_temperature_available=False,
        effect_variant_options=(),
        boost_state=None,
        fan_state=None,
        available_fan_modes=(),
        power_state=None,
        version_state=None,
        async_apply_turn_on=AsyncMock(),
        async_apply_effect=AsyncMock(),
        async_set_gm=AsyncMock(),
        async_set_effect_frequency=AsyncMock(),
        async_set_effect_kelvin=AsyncMock(),
        async_set_effect_variant=AsyncMock(),
        async_set_boost=AsyncMock(),
        async_set_boost_kelvin=AsyncMock(),
        async_set_fan_mode=AsyncMock(),
    )
    for key, value in runtime_values.items():
        setattr(runtime_data, key, value)
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


def test_effect_rate_entity_exposes_numeric_and_random_options() -> None:
    """The app's raw frequency value 11 is presented as Random."""
    entry = make_entry(
        {CONF_MODEL: PROFILE_ACE_25X},
        available=True,
        effect_state=EffectState(
            on=True,
            effect=SystemEffect.LIGHTNING,
            intensity=180,
            frequency=11,
        ),
        effect_frequency_available=True,
    )

    rate = select.AmaranEffectRateEntity(entry)

    assert rate.available
    assert rate.current_option == "random"
    assert rate.options == [*(str(value) for value in range(1, 11)), "random"]


@pytest.mark.asyncio
async def test_effect_rate_and_light_effect_forward_commands() -> None:
    """Effect selection and rate delegate atomically to the device layer."""
    entry = make_entry(
        {CONF_MODEL: PROFILE_ACE_25X},
        available=True,
        effect_state=EffectState(
            on=True,
            effect=SystemEffect.TV,
            intensity=180,
            frequency=4,
        ),
        effect_frequency_available=True,
        effect_variant_options=("warmer", "natural", "cooler"),
    )

    rate = select.AmaranEffectRateEntity(entry)
    color = select.AmaranEffectColorEntity(entry)
    await rate.async_select_option("random")
    await color.async_select_option("cooler")
    light_entity = light.AmaranLightEntity(entry)
    await light_entity.async_turn_on(**{ATTR_EFFECT: SystemEffect.FIRE.value})

    entry.runtime_data.async_set_effect_frequency.assert_awaited_once_with(11)
    entry.runtime_data.async_set_effect_variant.assert_awaited_once_with("cooler")
    assert color.options == ["warmer", "natural", "cooler"]
    assert color.current_option == "warmer"
    entry.runtime_data.async_apply_effect.assert_awaited_once_with(
        SystemEffect.FIRE.value, intensity=None
    )
    assert light_entity.supported_features & LightEntityFeature.EFFECT
    assert light_entity.effect == SystemEffect.TV.value
    assert light_entity.supported_color_modes == {ColorMode.COLOR_TEMP}
    assert light_entity.color_mode is ColorMode.BRIGHTNESS

    light_entity.entity_id = "light.test"
    attributes = light_entity.state_attributes
    assert attributes["effect"] == SystemEffect.TV.value
    assert attributes["color_mode"] is ColorMode.BRIGHTNESS
    assert attributes["brightness"] == 46
    assert attributes["color_temp_kelvin"] is None


@pytest.mark.asyncio
async def test_effect_and_boost_cct_numbers_use_profile_ranges() -> None:
    """Ace's app-visible CCT controls use their distinct verified ranges."""
    effect = EffectState(
        on=True,
        effect=SystemEffect.LIGHTNING,
        intensity=180,
        frequency=4,
        kelvin=4300,
    )
    entry = make_entry(
        {CONF_MODEL: PROFILE_ACE_25X},
        available=True,
        effect_state=effect,
        effect_color_temperature_available=True,
        boost_state=SimpleNamespace(enabled=True, kelvin=5000),
    )

    effect_cct = number.AmaranEffectColorTemperatureEntity(entry)
    boost_cct = number.AmaranBoostColorTemperatureEntity(entry)

    assert effect_cct.available
    assert effect_cct.native_value == 4300
    assert effect_cct.native_min_value == 2700
    assert effect_cct.native_max_value == 6500
    assert effect_cct.native_step == 50
    assert effect_cct.native_unit_of_measurement is UnitOfTemperature.KELVIN
    assert boost_cct.available
    assert boost_cct.native_value == 5000
    assert boost_cct.native_min_value == 3800
    assert boost_cct.native_max_value == 5500

    await effect_cct.async_set_native_value(4325)
    await boost_cct.async_set_native_value(5025)
    entry.runtime_data.async_set_effect_kelvin.assert_awaited_once_with(4325)
    entry.runtime_data.async_set_boost_kelvin.assert_awaited_once_with(5025)


@pytest.mark.asyncio
async def test_boost_and_fan_entities_use_device_state() -> None:
    """Boost tracks its modal write while fan exposes only reported modes."""
    entry = make_entry(
        {CONF_MODEL: PROFILE_ACE_25X},
        available=True,
        boost_state=SimpleNamespace(enabled=True),
        fan_state=SimpleNamespace(mode=SimpleNamespace(value="smart")),
        available_fan_modes=("silent", "smart"),
    )

    boost = switch.AmaranBoostEntity(entry)
    fan = select.AmaranFanModeEntity(entry)
    assert boost.available
    assert boost.is_on
    assert fan.options == ["silent", "smart"]
    assert fan.current_option == "smart"
    assert fan.entity_category is EntityCategory.CONFIG

    await boost.async_turn_off()
    await fan.async_select_option("silent")
    entry.runtime_data.async_set_boost.assert_awaited_once_with(False)
    entry.runtime_data.async_set_fan_mode.assert_awaited_once_with("silent")


def test_power_and_version_sensor_metadata_and_values() -> None:
    """Ace diagnostics use Home Assistant-native classes and units."""
    entry = make_entry(
        {CONF_MODEL: PROFILE_ACE_25X},
        available=True,
        power_state=SimpleNamespace(
            battery_percent=82,
            runtime_minutes=47,
            source=SimpleNamespace(value="battery"),
        ),
        version_state=SimpleNamespace(
            protocol_version=42,
            control_hw_version=1,
            control_sw_version=2,
            driver_hw_version=3,
            driver_sw_version=4,
            cct_min_kelvin=2700,
            cct_max_kelvin=6500,
        ),
        fan_state=SimpleNamespace(
            fixture_speed=777,
            temperature_c=31,
            high_temperature_raw=0,
        ),
    )

    battery = sensor.AmaranBatteryEntity(entry)
    runtime = sensor.AmaranRemainingRuntimeEntity(entry)
    source = sensor.AmaranPowerSourceEntity(entry)
    version = sensor.AmaranProtocolVersionEntity(entry)
    fan_speed = sensor.AmaranFanSpeedEntity(entry)
    fan_temperature = sensor.AmaranFanTemperatureEntity(entry)

    assert battery.native_value == 82
    assert battery.device_class is SensorDeviceClass.BATTERY
    assert battery.native_unit_of_measurement == PERCENTAGE
    assert battery.state_class is SensorStateClass.MEASUREMENT
    assert runtime.native_value == 47
    assert runtime.device_class is SensorDeviceClass.DURATION
    assert runtime.native_unit_of_measurement is UnitOfTime.MINUTES
    assert source.native_value == "battery"
    assert source.device_class is SensorDeviceClass.ENUM
    assert version.native_value == "42"
    assert version.extra_state_attributes["driver_software_version"] == 4
    assert battery.entity_category is EntityCategory.DIAGNOSTIC
    assert fan_speed.native_value == 777
    assert fan_speed.native_unit_of_measurement == REVOLUTIONS_PER_MINUTE
    assert fan_temperature.native_value == 31
    assert fan_temperature.native_unit_of_measurement is UnitOfTemperature.CELSIUS
    assert fan_temperature.extra_state_attributes == {"high_temperature": False}


def test_optional_entities_do_not_depend_on_primary_light_report() -> None:
    """Typed diagnostic pages remain usable if only primary status is missing."""
    entry = make_entry(
        {CONF_MODEL: PROFILE_ACE_25X},
        available=False,
        connected=True,
        boost_state=SimpleNamespace(enabled=False, kelvin=4300),
        fan_state=SimpleNamespace(
            mode=SimpleNamespace(value="smart"),
            fixture_speed=700,
            temperature_c=30,
            high_temperature_raw=0,
        ),
        available_fan_modes=("silent", "smart"),
        power_state=SimpleNamespace(
            battery_percent=50,
            runtime_minutes=30,
            source=SimpleNamespace(value="battery"),
        ),
        version_state=SimpleNamespace(protocol_version=42),
    )

    assert switch.AmaranBoostEntity(entry).available
    assert select.AmaranFanModeEntity(entry).available
    assert sensor.AmaranBatteryEntity(entry).available
    assert sensor.AmaranProtocolVersionEntity(entry).available


@pytest.mark.asyncio
async def test_generic_profile_removes_every_model_specific_entity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Changing Ace to Generic cleans every stale entity-registry surface."""
    removed: list[str] = []

    class Registry:
        def async_get_entity_id(
            self, _domain: object, _platform: str, unique_id: str
        ) -> str:
            return unique_id

        def async_remove(self, entity_id: str) -> None:
            removed.append(entity_id)

    registry = Registry()
    monkeypatch.setattr(number.er, "async_get", lambda _hass: registry)
    entry = make_entry({CONF_MODEL: PROFILE_GENERIC})
    add_entities = Mock()

    await number.async_setup_entry(object(), entry, add_entities)
    await select.async_setup_entry(object(), entry, add_entities)
    await sensor.async_setup_entry(object(), entry, add_entities)
    await switch.async_setup_entry(object(), entry, add_entities)

    address = entry.data[CONF_ADDRESS]
    assert set(removed) == {
        f"{address}_gm",
        f"{address}_effect_cct",
        f"{address}_boost_cct",
        f"{address}_fan_mode",
        f"{address}_effect_rate",
        f"{address}_effect_color",
        f"{address}_battery",
        f"{address}_remaining_runtime",
        f"{address}_power_source",
        f"{address}_protocol_version",
        f"{address}_fan_speed",
        f"{address}_fan_temperature",
        f"{address}_boost",
    }
    add_entities.assert_not_called()
