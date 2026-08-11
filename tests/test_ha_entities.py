"""Home Assistant entity behavior for configured fixture capabilities."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
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
from custom_components.amaran_ble.amaranble import highspeed, pixelfx, systemfx2, telink
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
from custom_components.amaran_ble.profiles import (
    get_fixture_profile,
    get_fixture_profile_by_product_id,
)


def test_light_platform_allows_device_level_latest_wins() -> None:
    """HA must not queue slider calls before the device can coalesce them."""
    assert light.PARALLEL_UPDATES == 0


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
        green_magenta_min=-10,
        green_magenta_max=10,
        effect_state=None,
        effect_frequency_available=False,
        effect_color_temperature_available=False,
        effect_hue_available=False,
        effect_saturation_available=False,
        effect_gm_available=False,
        effect_variant_options=(),
        effect_color_mode_options=(),
        effect_color_mode=None,
        boost_state=None,
        fan_state=None,
        available_fan_modes=(),
        power_state=None,
        version_state=None,
        version2_state=None,
        high_speed_state=None,
        async_apply_turn_on=AsyncMock(),
        async_apply_effect=AsyncMock(),
        async_set_gm=AsyncMock(),
        async_set_effect_frequency=AsyncMock(),
        async_set_effect_kelvin=AsyncMock(),
        async_set_effect_hue=AsyncMock(),
        async_set_effect_saturation=AsyncMock(),
        async_set_effect_gm=AsyncMock(),
        async_set_effect_variant=AsyncMock(),
        async_set_effect_color_mode=AsyncMock(),
        async_set_boost=AsyncMock(),
        async_set_boost_kelvin=AsyncMock(),
        async_set_boost_gm=AsyncMock(),
        async_set_fan_mode=AsyncMock(),
        async_set_fan_speed=AsyncMock(),
        async_set_high_speed=AsyncMock(),
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


def test_system_effect2_is_exposed_as_a_normal_home_assistant_effect() -> None:
    """Command-34 state uses HA's standard effect, power, and brightness surface."""
    profile = get_fixture_profile_by_product_id("000G5")
    state = systemfx2.decode_effect2(
        systemfx2.effect2(
            systemfx2.SystemEffect2.LIGHTNING_II,
            intensity=321,
        )[0]
    )
    assert state is not None
    entry = make_entry(
        {CONF_MODEL: profile.key},
        available=True,
        effect_state=state,
    )
    entity = light.AmaranLightEntity(entry)

    assert entity.effect == "Lightning II"
    assert entity.is_on
    assert entity.brightness == round(321 * 255 / 1000)
    assert entity.color_mode is ColorMode.BRIGHTNESS
    assert "Lightning II" in entity.effect_list
    assert "Lightning III" not in entity.effect_list


def test_pixel_effect_is_safe_across_light_and_inactive_parameter_entities() -> None:
    """A pixel primary state cannot break legacy FX entity property reads."""
    profile = get_fixture_profile_by_product_id("000F5")
    state = SimpleNamespace(
        on=True,
        effect=pixelfx.PixelEffect.RAINBOW,
        intensity=321,
    )
    entry = make_entry(
        {CONF_MODEL: profile.key},
        available=True,
        effect_state=state,
    )
    entity = light.AmaranLightEntity(entry)

    assert entity.effect == "Rainbow"
    assert entity.is_on
    assert entity.brightness == round(321 * 255 / 1000)
    assert entity.color_mode is ColorMode.BRIGHTNESS
    assert "Rainbow" in entity.effect_list

    assert select.AmaranEffectRateEntity(entry).current_option is None
    assert select.AmaranEffectColorEntity(entry).current_option is None
    assert number.AmaranEffectColorTemperatureEntity(entry).native_value is None
    assert number.AmaranEffectHueEntity(entry).native_value is None
    assert number.AmaranEffectSaturationEntity(entry).native_value is None
    assert number.AmaranEffectGreenMagentaEntity(entry).native_value is None


def test_pixel_brightness_clamps_ten_bit_report_to_ha_range() -> None:
    """Raw command-33 intensity 1023 must not exceed HA brightness 255."""
    profile = get_fixture_profile_by_product_id("000F5")
    state = SimpleNamespace(
        on=True,
        effect=pixelfx.PixelEffect.RAINBOW,
        intensity=1023,
    )
    entry = make_entry(
        {CONF_MODEL: profile.key},
        available=True,
        effect_state=state,
    )

    assert light.AmaranLightEntity(entry).brightness == 255


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


def test_effect_rate_omits_random_for_ten_step_effects() -> None:
    """Effects whose app slider ends at ten must not offer wire value eleven."""
    entry = make_entry(
        {CONF_MODEL: "ace_25c"},
        available=True,
        effect_state=EffectState(
            on=True,
            effect=SystemEffect.COLOR_CHASE,
            intensity=180,
            frequency=5,
            saturation=100,
        ),
        effect_frequency_available=True,
    )

    assert select.AmaranEffectRateEntity(entry).options == [
        str(value) for value in range(1, 11)
    ]


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
async def test_effect_color_mode_select_follows_active_legacy_effect() -> None:
    """A dual-color effect exposes its live CCT/HSI representation."""
    entry = make_entry(
        {CONF_MODEL: "ace_25c"},
        available=True,
        connected=True,
        effect_state=EffectState(
            on=True,
            effect=SystemEffect.FAULTY_BULB,
            intensity=180,
            frequency=5,
            mode=0,
            kelvin=4300,
            gm=100,
        ),
        effect_color_mode_options=("cct", "hsi"),
        effect_color_mode="cct",
    )
    entity = select.AmaranEffectColorModeEntity(entry)

    assert entity.available
    assert entity.options == ["cct", "hsi"]
    assert entity.current_option == "cct"

    await entity.async_select_option("hsi")
    entry.runtime_data.async_set_effect_color_mode.assert_awaited_once_with("hsi")

    entry.runtime_data.effect_color_mode_options = ()
    entry.runtime_data.effect_color_mode = None
    assert not entity.available
    assert entity.current_option is None


@pytest.mark.asyncio
async def test_full_color_profile_registers_effect_color_mode_select(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A named dual-mode fixture gets the selector before an effect is active."""

    class Registry:
        def async_get_entity_id(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(select.er, "async_get", lambda _hass: Registry())
    entry = make_entry({CONF_MODEL: "ace_25c"})
    add_entities = Mock()

    await select.async_setup_entry(object(), entry, add_entities)

    assert any(
        isinstance(item, select.AmaranEffectColorModeEntity)
        for item in add_entities.call_args.args[0]
    )


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
async def test_effect_hue_and_saturation_numbers_follow_active_report() -> None:
    """HSI effect parameters are exposed only while present in a live report."""
    effect = EffectState(
        on=True,
        effect=SystemEffect.WELDING,
        intensity=180,
        frequency=5,
        speed=18,
        trigger=2,
        mode=1,
        hue=120,
        saturation=75,
    )
    entry = make_entry(
        {CONF_MODEL: "ace_25c"},
        available=True,
        effect_state=effect,
        effect_hue_available=True,
        effect_saturation_available=True,
    )
    hue = number.AmaranEffectHueEntity(entry)
    saturation = number.AmaranEffectSaturationEntity(entry)

    assert hue.available and hue.native_value == 120
    assert hue.native_min_value == 0 and hue.native_max_value == 360
    assert saturation.available and saturation.native_value == 75
    assert saturation.native_min_value == 0
    assert saturation.native_max_value == 100

    await hue.async_set_native_value(121.5)
    await saturation.async_set_native_value(76.5)
    entry.runtime_data.async_set_effect_hue.assert_awaited_once_with(121.5)
    entry.runtime_data.async_set_effect_saturation.assert_awaited_once_with(76.5)


@pytest.mark.asyncio
async def test_ace_25c_boost_tint_uses_normalized_green_magenta_scale() -> None:
    """The app's raw 0..200 Boost tint is shown consistently as -10..+10."""
    entry = make_entry(
        {CONF_MODEL: "ace_25c"},
        available=True,
        boost_state=SimpleNamespace(enabled=True, kelvin=4500, gm=125),
    )
    entity = number.AmaranBoostGreenMagentaEntity(entry)

    assert entity.available
    assert entity.native_value == 2.5
    assert entity.native_min_value == -10
    assert entity.native_max_value == 10
    await entity.async_set_native_value(-2.5)
    entry.runtime_data.async_set_boost_gm.assert_awaited_once_with(-2.5)


@pytest.mark.asyncio
async def test_effect_tint_uses_normalized_green_magenta_scale() -> None:
    """The effect's raw 0..200 tint is shown consistently as -10..+10."""
    entry = make_entry(
        {CONF_MODEL: "ace_25c"},
        available=True,
        effect_state=EffectState(
            on=True,
            effect=SystemEffect.LIGHTNING,
            intensity=180,
            frequency=5,
            kelvin=4300,
            gm=125,
        ),
        effect_gm_available=True,
    )
    entity = number.AmaranEffectGreenMagentaEntity(entry)

    assert entity.available
    assert entity.native_value == 2.5
    assert entity.native_min_value == -10
    assert entity.native_max_value == 10
    await entity.async_set_native_value(-2.5)
    entry.runtime_data.async_set_effect_gm.assert_awaited_once_with(-2.5)

    gm_v2_profile = get_fixture_profile_by_product_id("05010")
    gm_v2_entry = make_entry({CONF_MODEL: gm_v2_profile.key})
    assert number.AmaranGreenMagentaEntity(gm_v2_entry).native_step == 0.1
    assert number.AmaranEffectGreenMagentaEntity(gm_v2_entry).native_step == 0.1


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


@pytest.mark.asyncio
async def test_manual_fan_speed_is_report_gated_and_writable() -> None:
    """Manual RPM is exposed only in a confirmed Manual fan session."""
    profile = get_fixture_profile_by_product_id("000G5")
    fan_state = telink.FanState(
        mode=telink.FanMode.MANUAL,
        fixture_speed=650,
        current_temperature_raw=0,
        high_temperature_raw=0,
        supported_modes=(telink.FanMode.MANUAL,),
    )
    entry = make_entry(
        {CONF_MODEL: profile.key},
        available=False,
        connected=True,
        fan_state=fan_state,
    )
    entity = number.AmaranFanManualSpeedEntity(entry)

    assert entity.available
    assert entity.native_value == 650
    assert entity.native_min_value == 0
    assert entity.native_max_value == 1000
    assert entity.native_unit_of_measurement == REVOLUTIONS_PER_MINUTE
    assert entity.entity_category is EntityCategory.CONFIG
    await entity.async_set_native_value(725.4)
    entry.runtime_data.async_set_fan_speed.assert_awaited_once_with(725.4)

    entry.runtime_data.fan_state = telink.FanState(
        mode=telink.FanMode.SMART,
        fixture_speed=650,
        current_temperature_raw=0,
        high_temperature_raw=0,
        supported_modes=(telink.FanMode.MANUAL, telink.FanMode.SMART),
    )
    assert not entity.available


@pytest.mark.asyncio
async def test_high_speed_photography_switch_uses_catalog_limits() -> None:
    """Cataloged command-53 models expose a typed local-mode switch."""
    profile = get_fixture_profile_by_product_id("40145")
    state = highspeed.HighSpeedMessage(
        highspeed.HighSpeedState.ON,
        highspeed.HighSpeedOperation.APP_DEFAULT,
    )
    entry = make_entry(
        {CONF_MODEL: profile.key},
        available=False,
        connected=True,
        high_speed_state=state,
    )

    entity = switch.AmaranHighSpeedPhotographyEntity(entry)

    assert entity.available
    assert entity.assumed_state
    assert entity.is_on
    assert entity.extra_state_attributes == {
        "minimum_intensity_percent": 50,
        "maximum_intensity_percent": 100,
    }
    await entity.async_turn_off()
    entry.runtime_data.async_set_high_speed.assert_awaited_once_with(False)


@pytest.mark.asyncio
async def test_system_effect2_profile_registers_its_parameter_entities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Command-34 field metadata creates controls before the effect is active."""

    class Registry:
        def async_get_entity_id(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(number.er, "async_get", lambda _hass: Registry())
    profile = get_fixture_profile_by_product_id("000G5")
    entry = make_entry({CONF_MODEL: profile.key}, available=True)
    add_entities = Mock()

    await number.async_setup_entry(object(), entry, add_entities)

    entities = add_entities.call_args.args[0]
    assert any(
        isinstance(item, number.AmaranEffectColorTemperatureEntity) for item in entities
    )
    assert any(isinstance(item, number.AmaranEffectHueEntity) for item in entities)
    assert any(
        isinstance(item, number.AmaranEffectSaturationEntity) for item in entities
    )
    assert any(
        isinstance(item, number.AmaranEffectGreenMagentaEntity) for item in entities
    )
    assert any(isinstance(item, number.AmaranFanManualSpeedEntity) for item in entities)


@pytest.mark.asyncio
async def test_ace_does_not_register_an_unsupported_manual_fan_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ace's verified Silent/Smart fan report must not create a dead RPM control."""

    class Registry:
        def async_get_entity_id(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(number.er, "async_get", lambda _hass: Registry())
    entry = make_entry({CONF_MODEL: PROFILE_ACE_25X})
    add_entities = Mock()

    await number.async_setup_entry(object(), entry, add_entities)

    assert not any(
        isinstance(item, number.AmaranFanManualSpeedEntity)
        for item in add_entities.call_args.args[0]
    )


@pytest.mark.asyncio
async def test_effect_cct_number_requires_a_selectable_kelvin_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NOVA effect sets without a Kelvin packet never get a dead CCT entity."""

    class Registry:
        def async_get_entity_id(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(number.er, "async_get", lambda _hass: Registry())
    profile = get_fixture_profile_by_product_id("02065")
    entry = make_entry({CONF_MODEL: profile.key}, available=True)
    add_entities = Mock()

    await number.async_setup_entry(object(), entry, add_entities)

    entities = add_entities.call_args.args[0]
    assert not any(
        isinstance(item, number.AmaranEffectColorTemperatureEntity) for item in entities
    )


@pytest.mark.asyncio
async def test_full_color_legacy_effects_register_hsi_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cmd7 HSI branches expose hue/saturation controls on colour fixtures."""

    class Registry:
        def async_get_entity_id(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(number.er, "async_get", lambda _hass: Registry())
    entry = make_entry({CONF_MODEL: "ace_25c"}, available=True)
    add_entities = Mock()

    await number.async_setup_entry(object(), entry, add_entities)

    entities = add_entities.call_args.args[0]
    assert any(isinstance(item, number.AmaranEffectHueEntity) for item in entities)
    assert any(
        isinstance(item, number.AmaranEffectSaturationEntity) for item in entities
    )


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
            manual_fx_supported=False,
            program_fx_supported=True,
            picker_fx_supported=True,
            touchbar_fx_supported=False,
            music_fx_supported=True,
        ),
        version2_state=SimpleNamespace(
            system_effects_2_supported=True,
            active_system_effect_groups=("A", "C"),
            pixel_effects_supported=True,
            active_pixel_effect_groups=("B",),
            pixel_x1=4,
            pixel_y1=1,
            pixel_x2=24,
            pixel_y2=1,
            pixel_num=4,
            motion_supported=False,
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
    assert version.extra_state_attributes["program_effects_supported"] is True
    assert version.extra_state_attributes["touchbar_effects_supported"] is False
    assert version.extra_state_attributes["system_effect_groups"] == ["A", "C"]
    assert version.extra_state_attributes["pixel_geometry"] == [[4, 1], [24, 1]]
    assert battery.entity_category is EntityCategory.DIAGNOSTIC
    assert fan_speed.native_value == 777
    assert fan_speed.native_unit_of_measurement == REVOLUTIONS_PER_MINUTE
    assert fan_temperature.native_value == 31
    assert fan_temperature.native_unit_of_measurement is UnitOfTemperature.CELSIUS
    assert fan_temperature.extra_state_attributes == {"high_temperature": False}


@pytest.mark.parametrize(
    ("reported_percent", "expected_percent"),
    [(0, 0), (100, 100), (101, 100), (127, 100)],
)
def test_battery_sensor_caps_seven_bit_report_at_100_percent(
    reported_percent: int, expected_percent: int
) -> None:
    """Seven-bit fixture values follow the app's full-battery presentation."""
    entry = make_entry(
        {CONF_MODEL: PROFILE_ACE_25X},
        available=True,
        power_state=SimpleNamespace(battery_percent=reported_percent),
    )

    battery = sensor.AmaranBatteryEntity(entry)

    assert battery.available
    assert battery.native_value == expected_percent


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
async def test_named_nonfan_profile_removes_stale_fan_sensors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Changing named models removes diagnostics absent from the new profile."""
    removed: list[str] = []

    class Registry:
        def async_get_entity_id(
            self, _domain: object, _platform: str, unique_id: str
        ) -> str:
            return unique_id

        def async_remove(self, entity_id: str) -> None:
            removed.append(entity_id)

    monkeypatch.setattr(sensor.er, "async_get", lambda _hass: Registry())
    profile = get_fixture_profile_by_product_id("400B5")  # amaran F21x
    entry = make_entry({CONF_MODEL: profile.key}, available=True)
    add_entities = Mock()

    await sensor.async_setup_entry(object(), entry, add_entities)

    address = entry.data[CONF_ADDRESS]
    assert removed == [
        f"{address}_fan_speed",
        f"{address}_fan_temperature",
    ]
    entities = add_entities.call_args.args[0]
    assert {type(entity) for entity in entities} == {
        sensor.AmaranBatteryEntity,
        sensor.AmaranRemainingRuntimeEntity,
        sensor.AmaranPowerSourceEntity,
        sensor.AmaranProtocolVersionEntity,
    }


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
        f"{address}_effect_hue",
        f"{address}_effect_saturation",
        f"{address}_effect_gm",
        f"{address}_boost_cct",
        f"{address}_boost_gm",
        f"{address}_fan_manual_speed",
        f"{address}_fan_mode",
        f"{address}_effect_rate",
        f"{address}_effect_color",
        f"{address}_effect_color_mode",
        f"{address}_battery",
        f"{address}_remaining_runtime",
        f"{address}_power_source",
        f"{address}_protocol_version",
        f"{address}_fan_speed",
        f"{address}_fan_temperature",
        f"{address}_boost",
        f"{address}_high_speed_photography",
    }
    add_entities.assert_not_called()
