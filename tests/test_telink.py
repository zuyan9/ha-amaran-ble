"""Golden vectors for amaran's Telink opcode 0x26 payload."""

from __future__ import annotations

import pytest
from amaranble import telink


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (telink.status_request(), "0e00000000000000000e"),
        (telink.onoff(False), "8c00000000000000008c"),
        (telink.onoff(True), "8d00000000000000018c"),
        (telink.brightness(0), "8f00000000000000008f"),
        (telink.brightness(1), "cf00000000000040008f"),
        (telink.brightness(500), "0c000000000000007d8f"),
        (telink.brightness(1000), "8900000000000000fa8f"),
        (telink.cct(2700, 1), "f30000000040e1500082"),
        (telink.cct(5600, 800), "ae00000000400123c882"),
        (telink.cct(6500, 1000), "850000000040a128fa82"),
        (telink.cct(10000, 500), "fe0000000040813e7d82"),
        (telink.cct(10010, 500), "54000000004411007d82"),
        (telink.cct(15000, 500), "a30000000044411f7d82"),
        (telink.cct(20000, 500), "020000000044813e7d82"),
        (telink.hsi(45, 60, 800), "fd0000000000af05c881"),
        (telink.hsi(360, 100, 1000), "c10000000000192dfa81"),
    ],
)
def test_command_vectors(payload: bytes, expected: str) -> None:
    assert payload.hex() == expected
    assert payload[0] == sum(payload[1:]) & 0xFF


def _as_report(command: bytes, *, on: bool) -> bytes:
    payload = bytearray(command)
    payload[9] &= 0x7F
    payload[1] = (payload[1] & ~1) | on
    payload[0] = sum(payload[1:]) & 0xFF
    return bytes(payload)


def test_decode_cct_status() -> None:
    state = telink.decode_status(_as_report(telink.cct(5600, 800, -4), on=True))
    assert state == telink.LightState(
        on=True,
        is_hsi=False,
        intensity=800,
        kelvin=5600,
        gm=-4,
        hue=0,
        saturation=0,
    )


@pytest.mark.parametrize("kelvin", [10000, 10010, 15000, 20000])
def test_decode_high_cct_status(kelvin: int) -> None:
    state = telink.decode_status(_as_report(telink.cct(kelvin, 500), on=True))
    assert state is not None
    assert state.kelvin == kelvin


def test_decode_hsi_status() -> None:
    state = telink.decode_status(_as_report(telink.hsi(315, 73, 421), on=False))
    assert state == telink.LightState(
        on=False,
        is_hsi=True,
        intensity=421,
        kelvin=0,
        gm=0,
        hue=315,
        saturation=73,
    )


def test_bounds_are_clamped() -> None:
    assert telink.brightness(-1) == telink.brightness(0)
    assert telink.brightness(1001) == telink.brightness(1000)
    assert telink.cct(0, 500) == telink.cct(telink.MIN_KELVIN, 500)
    assert telink.cct(50000, 500) == telink.cct(telink.MAX_KELVIN, 500)
    assert telink.hsi(-1, -1, -1) == telink.hsi(0, 0, 0)


def test_command_values_use_javascript_half_up_rounding() -> None:
    assert telink.brightness(0.5) == telink.brightness(1)
    assert telink.cct(5600, 500.5, 0.5) == telink.cct(5600, 501, 1)
    assert telink.cct(5600, 500, -1.5) == telink.cct(5600, 500, -1)
    assert telink.hsi(44.5, 60.5, 800.5) == telink.hsi(45, 61, 801)


def test_decode_rejects_invalid_or_non_state_payloads() -> None:
    invalid = bytearray(_as_report(telink.cct(5000, 500), on=True))
    invalid[3] ^= 1
    assert telink.decode_status(bytes(invalid)) is None
    assert telink.decode_status(b"short") is None

    diagnostic = bytearray(10)
    diagnostic[9] = 0x0A
    diagnostic[0] = 0x0A
    assert telink.decode_status(bytes(diagnostic)) is None


@pytest.mark.parametrize(
    ("effect", "expected"),
    [
        (telink.SystemEffect.OFF, "96000000000000000f87"),
        (telink.SystemEffect.PAPARAZZI, "100100001430162d0187"),
        (telink.SystemEffect.FIREWORKS, "d70100000000142d0e87"),
        (telink.SystemEffect.FAULTY_BULB, "b801805201232d050887"),
        (telink.SystemEffect.LIGHTNING, "3a0100281530162d0287"),
        (telink.SystemEffect.TV, "cc0100000000142d0387"),
        (telink.SystemEffect.PULSING, "b901805201232d050987"),
        (telink.SystemEffect.STROBE, "3401005001232d050687"),
        (telink.SystemEffect.EXPLOSION, "2d01004801232d050787"),
        (telink.SystemEffect.FIRE, "ce0100000000142d0587"),
    ],
)
def test_default_ace_effect_vectors(effect: telink.SystemEffect, expected: str) -> None:
    payload = telink.effect(effect)
    assert payload.hex() == expected
    assert payload[0] == sum(payload[1:]) & 0xFF


@pytest.mark.parametrize(
    ("effect", "kwargs", "expected", "state"),
    [
        (
            telink.SystemEffect.PAPARAZZI,
            {"intensity": 900, "frequency": 11, "kelvin": 6500},
            "36010000148a2ee10187",
            telink.EffectState(
                True, telink.SystemEffect.PAPARAZZI, 900, 11, kelvin=6500
            ),
        ),
        (
            telink.SystemEffect.FIREWORKS,
            {"intensity": 901, "frequency": 11, "variant": 2},
            "eb01000000086ce10e87",
            telink.EffectState(True, telink.SystemEffect.FIREWORKS, 901, 11, variant=2),
        ),
        (
            telink.SystemEffect.FAULTY_BULB,
            {
                "intensity": 902,
                "frequency": 11,
                "speed": 10,
                "trigger": 3,
                "kelvin": 6500,
            },
            "2201005da1a8e10b0887",
            telink.EffectState(
                True,
                telink.SystemEffect.FAULTY_BULB,
                902,
                11,
                speed=10,
                trigger=3,
                kelvin=6500,
            ),
        ),
        (
            telink.SystemEffect.LIGHTNING,
            {
                "intensity": 903,
                "frequency": 11,
                "speed": 10,
                "trigger": 3,
                "kelvin": 6500,
            },
            "c80100d0158aeee10287",
            telink.EffectState(
                True,
                telink.SystemEffect.LIGHTNING,
                903,
                11,
                speed=10,
                trigger=3,
                kelvin=6500,
            ),
        ),
        (
            telink.SystemEffect.TV,
            {"intensity": 904, "frequency": 11, "variant": 2},
            "9b01000000022ce20387",
            telink.EffectState(True, telink.SystemEffect.TV, 904, 11, variant=2),
        ),
        (
            telink.SystemEffect.PULSING,
            {
                "intensity": 905,
                "frequency": 11,
                "speed": 10,
                "trigger": 3,
                "kelvin": 6500,
            },
            "e401005da168e20b0987",
            telink.EffectState(
                True,
                telink.SystemEffect.PULSING,
                905,
                11,
                speed=10,
                trigger=3,
                kelvin=6500,
            ),
        ),
        (
            telink.SystemEffect.STROBE,
            {"intensity": 906, "frequency": 11, "trigger": 3, "kelvin": 6500},
            "1c010058a1a8e20b0687",
            telink.EffectState(
                True,
                telink.SystemEffect.STROBE,
                906,
                11,
                trigger=3,
                kelvin=6500,
            ),
        ),
        (
            telink.SystemEffect.EXPLOSION,
            {"intensity": 907, "frequency": 10, "trigger": 0, "kelvin": 6500},
            "44010040a1e8e20a0787",
            telink.EffectState(
                True,
                telink.SystemEffect.EXPLOSION,
                907,
                10,
                trigger=0,
                kelvin=6500,
            ),
        ),
        (
            telink.SystemEffect.FIRE,
            {"intensity": 908, "frequency": 10, "variant": 1},
            "99010000000128e30587",
            telink.EffectState(True, telink.SystemEffect.FIRE, 908, 10, variant=1),
        ),
    ],
)
def test_ace_effect_field_vectors_and_round_trip(
    effect: telink.SystemEffect,
    kwargs: dict[str, int],
    expected: str,
    state: telink.EffectState,
) -> None:
    payload = telink.effect(effect, **kwargs)
    assert payload.hex() == expected
    assert telink.decode_effect(payload) == state
    assert telink.decode_report(payload) == state


def test_effect_off_and_sleep_report() -> None:
    assert telink.effect_off() == telink.effect(telink.SystemEffect.OFF)
    assert telink.decode_effect(telink.effect_off()) == telink.EffectState(
        on=False,
        effect=telink.SystemEffect.OFF,
        intensity=0,
        frequency=0,
    )

    sleeping = bytearray(telink.effect(telink.SystemEffect.PAPARAZZI))
    sleeping[1] &= ~1
    sleeping[9] &= 0x7F
    sleeping[0] = sum(sleeping[1:]) & 0xFF
    state = telink.decode_effect(bytes(sleeping))
    assert state is not None
    assert state.on is False


def test_effect_bounds_are_safe_for_ace_cct_mode() -> None:
    fire = telink.decode_effect(
        telink.effect(
            telink.SystemEffect.FIRE,
            intensity=2000,
            frequency=99,
            variant=99,
        )
    )
    assert fire == telink.EffectState(
        True,
        telink.SystemEffect.FIRE,
        1000,
        10,
        variant=2,
    )

    faulty = telink.decode_effect(
        telink.effect(
            telink.SystemEffect.FAULTY_BULB,
            intensity=-1,
            frequency=0,
            speed=99,
            trigger=99,
            kelvin=99999,
        )
    )
    assert faulty == telink.EffectState(
        True,
        telink.SystemEffect.FAULTY_BULB,
        0,
        1,
        speed=10,
        trigger=3,
        kelvin=6500,
    )


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (telink.boost(True, 3800), "63000000000040c697c6"),
        (telink.boost(False, 5500), "8e0000000000406622c6"),
        (telink.fan_request(), "09000000000000000009"),
        (telink.fan(telink.FanMode.MANUAL, 321), "cb000000000041010089"),
        (telink.fan(telink.FanMode.SMART, 321), "cc000000000041010189"),
        (telink.fan(telink.FanMode.SILENT, 321), "d2000000000041010789"),
        (telink.power_request(), "0b01000000000000000a"),
        (telink.version_request(), "00000000000000000000"),
    ],
)
def test_ace_auxiliary_command_vectors(payload: bytes, expected: str) -> None:
    assert payload.hex() == expected
    assert payload[0] == sum(payload[1:]) & 0xFF


def test_decode_boost() -> None:
    payload = telink.boost(True, 3800, 123)
    assert telink.decode_boost(payload) == telink.BoostState(True, 3800, 123)
    assert telink.boost(True, 0) == telink.boost(True, telink.ACE_BOOST_MIN_KELVIN)
    assert telink.boost(True, 99999) == telink.boost(True, telink.ACE_BOOST_MAX_KELVIN)


def test_decode_fan_capabilities_and_raw_temperature() -> None:
    # smart, 777 RPM, raw temperature 254 (-2 C), high-temp nibble 5;
    # capability bits advertise Smart and Silent.
    payload = bytes.fromhex("7800001054fe09030109")
    state = telink.decode_fan(payload)
    assert state == telink.FanState(
        mode=telink.FanMode.SMART,
        fixture_speed=777,
        current_temperature_raw=254,
        high_temperature_raw=5,
        supported_modes=(telink.FanMode.SMART, telink.FanMode.SILENT),
    )
    assert state.temperature_c == -2
    assert telink.decode_report(payload) == state

    unavailable = bytearray(payload)
    unavailable[5] = 128
    unavailable[0] = sum(unavailable[1:]) & 0xFF
    unavailable_state = telink.decode_fan(bytes(unavailable))
    assert unavailable_state is not None
    assert unavailable_state.temperature_c is None


def test_decode_legacy_and_protocol_42_power_reports() -> None:
    legacy = bytes.fromhex("6800102cabd039204e0a")
    assert telink.decode_power(legacy) == telink.PowerState(
        source=telink.PowerSource.EXTERNAL,
        battery_percent=85,
        runtime_minutes=300,
        battery_voltage_raw=14800,
        external_voltage_raw=20000,
    )

    current = bytes.fromhex("f80000779ba43800000a")
    state = telink.decode_power(current, protocol_version=42)
    assert state == telink.PowerState(
        source=telink.PowerSource.BATTERY,
        battery_percent=77,
        runtime_minutes=3000,
        battery_voltage_raw=14500,
        external_voltage_raw=0,
    )
    assert telink.decode_report(current, protocol_version=42) == state


def test_decode_version_report_and_ha_properties() -> None:
    payload = bytes.fromhex("a3b7cca7f40c6e62a900")
    state = telink.decode_version(payload)
    assert state == telink.VersionState(
        protocol_version=42,
        function=5,
        led_type=17,
        cct_low_raw=27,
        cct_high_raw=65,
        machine=9,
        manual_fx_supported=True,
        program_fx_supported=True,
        picker_fx_supported=True,
        touchbar_fx_supported=False,
        music_fx_supported=True,
        control_hardware_version_raw=10,
        control_software_version_raw=31,
        driver_hardware_version_raw=12,
        driver_software_version_raw=45,
        upgrade_type=1,
        gatt_version=1,
    )
    assert state.control_hw_version == "1.0"
    assert state.control_sw_version == "3.1"
    assert state.driver_hw_version == "1.2"
    assert state.driver_sw_version == "4.5"
    assert state.cct_min_kelvin == 2700
    assert state.cct_max_kelvin == 6500
    assert telink.decode_report(payload) == state


@pytest.mark.parametrize(
    "decoder",
    [
        telink.decode_effect,
        telink.decode_boost,
        telink.decode_fan,
        telink.decode_power,
        telink.decode_version,
        telink.decode_report,
    ],
)
def test_auxiliary_decoders_reject_bad_checksum(decoder) -> None:
    invalid = bytearray(telink.effect(telink.SystemEffect.PAPARAZZI))
    invalid[2] ^= 1
    assert decoder(bytes(invalid)) is None
    assert decoder(b"short") is None
