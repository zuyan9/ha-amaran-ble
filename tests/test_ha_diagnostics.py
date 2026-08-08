"""Home Assistant diagnostics tests for amaran BLE."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

from homeassistant.components.diagnostics import REDACTED
from homeassistant.config_entries import SOURCE_USER, ConfigEntry
from homeassistant.const import CONF_ADDRESS, CONF_NAME

from custom_components.amaran_ble import diagnostics
from custom_components.amaran_ble.amaranble import highspeed, telink
from custom_components.amaran_ble.const import (
    CONF_APP_KEY,
    CONF_DEVICE_KEY,
    CONF_INITIAL_SEQUENCE,
    CONF_IV_INDEX,
    CONF_LOCAL_ADDRESS,
    CONF_MODEL,
    CONF_NET_KEY,
    CONF_SEQUENCE_STORE_ID,
    CONF_UNICAST_ADDRESS,
    DOMAIN,
)
from custom_components.amaran_ble.profiles import get_fixture_profile_by_product_id


def _entry() -> ConfigEntry:
    """Build an exact Home Assistant config entry containing test secrets."""
    profile = get_fixture_profile_by_product_id("000G5")
    runtime_data = SimpleNamespace(
        profile=profile,
        connected=True,
        available=True,
        preferred_gm=1.5,
        available_fan_modes=("manual", "smart"),
        state=telink.LightState(
            on=True,
            is_hsi=False,
            intensity=640,
            kelvin=4300,
            gm=1.5,
            hue=0,
            saturation=0,
            gm_flag=True,
        ),
        effect_state=telink.EffectState(
            on=True,
            effect=telink.SystemEffect.LIGHTNING,
            intensity=500,
            frequency=5,
            kelvin=5600,
            gm=115,
            gm_flag=True,
        ),
        boost_state=telink.BoostState(enabled=False, kelvin=5000, gm=100),
        fan_state=telink.FanState(
            mode=telink.FanMode.SMART,
            fixture_speed=650,
            current_temperature_raw=31,
            high_temperature_raw=80,
            supported_modes=(telink.FanMode.MANUAL, telink.FanMode.SMART),
        ),
        power_state=telink.PowerState(
            source=telink.PowerSource.EXTERNAL,
            power_state_raw=True,
            battery_percent=82,
            runtime_minutes=145,
            battery_voltage_raw=1480,
            external_voltage_raw=2400,
        ),
        version_state=telink.VersionState(
            protocol_version=3,
            function=2,
            led_type=1,
            cct_low_raw=23,
            cct_high_raw=100,
            machine=4,
            manual_fx_supported=True,
            program_fx_supported=True,
            picker_fx_supported=True,
            touchbar_fx_supported=False,
            music_fx_supported=True,
            control_hardware_version_raw=12,
            control_software_version_raw=23,
            driver_hardware_version_raw=34,
            driver_software_version_raw=45,
            upgrade_type=1,
            gatt_version=1,
        ),
        version2_state=telink.Version2State(
            system_effects_2_supported=True,
            system_effect_groups=(True, False, True, False, False, False, False, False),
            pixel_effects_supported=True,
            pixel_effect_groups=(True, False, False, False, False, False, False),
            pixel_x1=4,
            pixel_y1=1,
            pixel_x2=24,
            pixel_y2=1,
            effect_active=True,
            sleeping=False,
            pixel_num=4,
            motion_supported=False,
        ),
        high_speed_state=highspeed.HighSpeedMessage(
            state=highspeed.HighSpeedState.ON,
            operation=highspeed.HighSpeedOperation.APP_DEFAULT,
        ),
    )
    fixed_time = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
    entry = ConfigEntry(
        created_at=fixed_time,
        modified_at=fixed_time,
        data={
            CONF_ADDRESS: "AA:BB:CC:DD:EE:FF",
            CONF_NAME: "Secret studio light",
            CONF_NET_KEY: "01" * 16,
            CONF_APP_KEY: "02" * 16,
            CONF_DEVICE_KEY: "03" * 16,
            CONF_SEQUENCE_STORE_ID: "secret-sequence-store",
            CONF_LOCAL_ADDRESS: 0x1234,
            CONF_UNICAST_ADDRESS: 0x5678,
            CONF_IV_INDEX: 0x10203040,
            CONF_INITIAL_SEQUENCE: 0x506070,
        },
        disabled_by=None,
        discovery_keys={},
        domain=DOMAIN,
        entry_id="secret-entry-id",
        minor_version=3,
        options={CONF_MODEL: profile.key},
        pref_disable_new_entities=None,
        pref_disable_polling=None,
        source=SOURCE_USER,
        state=None,
        subentries_data=(),
        title="Secret studio light",
        unique_id="AA:BB:CC:DD:EE:FF",
        version=1,
    )
    entry.runtime_data = runtime_data
    return entry


@pytest.mark.asyncio
async def test_diagnostics_redact_all_mesh_identity_and_sequence_data() -> None:
    """Diagnostics never reveal the fixture address or private mesh state."""
    result = await diagnostics.async_get_config_entry_diagnostics(object(), _entry())
    config = result["config_entry"]

    assert config["entry_id"] == REDACTED
    assert config["unique_id"] == REDACTED
    assert config["title"] == REDACTED
    assert config["discovery_keys"] == REDACTED
    for key in (
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
    ):
        assert config["data"][key] == REDACTED

    serialized = json.dumps(result, sort_keys=True)
    for secret in (
        "AA:BB:CC:DD:EE:FF",
        "Secret studio light",
        "01" * 16,
        "02" * 16,
        "03" * 16,
        "secret-sequence-store",
        "secret-entry-id",
        str(0x1234),
        str(0x5678),
        str(0x10203040),
        str(0x506070),
    ):
        assert secret not in serialized


@pytest.mark.asyncio
async def test_diagnostics_include_deterministic_profile_and_decoded_states() -> None:
    """Useful catalog and protocol state survives redaction in JSON-safe form."""
    first = await diagnostics.async_get_config_entry_diagnostics(object(), _entry())
    second = await diagnostics.async_get_config_entry_diagnostics(object(), _entry())

    assert first == second
    assert first["profile"]["key"] == "ls_600c_pro"
    assert first["profile"]["name"] == "LS 600c Pro"
    assert first["profile"]["catalog_capabilities"]["steady_color"] == {
        "hsi": True,
        "rgb": True,
        "xy": True,
        "gel": True,
        "advanced_hsi": True,
        "advanced_hsi_version": "1.5",
        "gm": True,
        "gm_min": 0,
        "gm_max": 20,
        "gm_v2_version": None,
    }

    runtime = first["runtime"]
    assert runtime["connected"] is True
    assert runtime["available"] is True
    assert runtime["preferred_green_magenta"] == 1.5
    assert runtime["available_fan_modes"] == ["manual", "smart"]
    assert runtime["states"]["light"] == {
        "type": "LightState",
        "data": {
            "on": True,
            "is_hsi": False,
            "intensity": 640,
            "kelvin": 4300,
            "gm": 1.5,
            "hue": 0,
            "saturation": 0,
            "gm_flag": True,
        },
    }
    assert runtime["states"]["effect"]["data"]["effect"] == "Lightning"
    assert runtime["states"]["fan"]["data"]["mode"] == "smart"
    assert runtime["states"]["fan"]["data"]["supported_modes"] == [
        "manual",
        "smart",
    ]
    assert runtime["states"]["power"]["data"]["source"] == "external"
    assert runtime["states"]["power"]["data"]["power_state_raw"] is True
    assert runtime["states"]["power"]["data"]["battery_percent"] == 82
    assert runtime["states"]["advanced_capabilities"]["data"][
        "system_effect_groups"
    ] == [True, False, True, False, False, False, False, False]
    assert runtime["states"]["high_speed_photography"]["data"] == {
        "state": 1,
        "operation": 0,
    }

    # Home Assistant's diagnostics encoder must be able to serialize everything.
    json.dumps(first, sort_keys=True)


@pytest.mark.asyncio
async def test_diagnostics_preserve_raw_battery_percentage() -> None:
    """Diagnostics retain a seven-bit value that the HA sensor caps at 100%."""
    entry = _entry()
    entry.runtime_data.power_state = telink.PowerState(
        source=telink.PowerSource.EXTERNAL,
        power_state_raw=False,
        battery_percent=127,
        runtime_minutes=145,
        battery_voltage_raw=1480,
        external_voltage_raw=2400,
    )

    result = await diagnostics.async_get_config_entry_diagnostics(object(), entry)

    assert result["runtime"]["states"]["power"]["data"]["battery_percent"] == 127
    assert result["runtime"]["states"]["power"]["data"]["power_state_raw"] is False
