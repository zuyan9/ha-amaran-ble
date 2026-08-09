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
        (
            telink.cct(5600, 800, -2.5, gm_flag=True),
            "de00000000680923c882",
        ),
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


def test_version2_request_vector() -> None:
    assert telink.version2_request().hex() == "25000000000000000025"


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


def test_decode_cct_gm_v2_status() -> None:
    """The app's flag/high-bit layout preserves tenths of G/M adjustment."""
    command = bytes.fromhex("de00000000680923c882")
    state = telink.decode_status(_as_report(command, on=True))

    assert state == telink.LightState(
        on=True,
        is_hsi=False,
        intensity=800,
        kelvin=5600,
        gm=-2.5,
        hue=0,
        saturation=0,
        gm_flag=True,
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


def test_nonfinite_numeric_inputs_follow_app_clamping() -> None:
    assert telink.brightness(float("inf")) == telink.brightness(1000)
    assert telink.brightness(float("-inf")) == telink.brightness(0)
    assert telink.brightness(float("nan")) == telink.brightness(0)
    assert telink.cct(float("nan"), 500) == telink.cct(telink.MIN_KELVIN, 500)
    assert telink.cct(5600, 500, float("nan")) == telink.cct(5600, 500, -10)
    assert telink.effect(
        telink.SystemEffect.PAPARAZZI, gm=float("nan")
    ) == telink.effect(telink.SystemEffect.PAPARAZZI, gm=0)


def test_command_values_use_javascript_half_up_rounding() -> None:
    assert telink.brightness(0.5) == telink.brightness(1)
    assert telink.cct(5600, 500.5, 0.5) == telink.cct(5600, 501, 0)
    assert telink.cct(5600, 500, -1.5) == telink.cct(5600, 500, -1)
    assert telink.hsi(44.5, 60.5, 800.5) == telink.hsi(45, 61, 801)


def test_cct_kelvin_uses_app_integer_division() -> None:
    assert telink.cct(5609, 500) == telink.cct(5600, 500)


@pytest.mark.parametrize("kelvin", [10001, 10005, 10009])
def test_legacy_effect_cct_wraps_only_after_raw_1000(kelvin: int) -> None:
    """The app divides Kelvin before testing its proprietary high-CCT flag."""
    payload = telink.effect(telink.SystemEffect.PAPARAZZI, kelvin=kelvin)
    baseline = telink.effect(telink.SystemEffect.PAPARAZZI, kelvin=10000)

    assert payload == baseline
    state = telink.decode_effect(payload)
    assert state is not None
    assert state.kelvin == 10000


def test_decode_rejects_invalid_or_non_state_payloads() -> None:
    invalid = bytearray(_as_report(telink.cct(5000, 500), on=True))
    invalid[3] ^= 1
    assert telink.decode_status(bytes(invalid)) is None
    assert telink.decode_status(b"short") is None

    diagnostic = bytearray(10)
    diagnostic[9] = 0x0A
    diagnostic[0] = 0x0A
    assert telink.decode_status(bytes(diagnostic)) is None


def test_decode_version2_capabilities_golden_vector() -> None:
    payload = bytes.fromhex("4d0000d08cc2cb6ad525")
    state = telink.Version2State(
        system_effects_2_supported=True,
        system_effect_groups=(True, False, True, False, True, False, True, False),
        pixel_effects_supported=True,
        pixel_effect_groups=(True, False, True, False, True, False, True),
        pixel_x1=9,
        pixel_y1=7,
        pixel_x2=33,
        pixel_y2=17,
        effect_active=True,
        sleeping=False,
        pixel_num=6,
        motion_supported=True,
    )
    assert telink.decode_version2(payload) == state
    assert telink.decode_report(payload) == state
    assert state.active_system_effect_groups == ("A", "C", "E", "G")
    assert state.active_pixel_effect_groups == ("A", "C", "E", "G")


def test_decode_version2_rejects_invalid_payload() -> None:
    invalid = bytearray.fromhex("4d0000d08cc2cb6ad525")
    invalid[2] ^= 1
    assert telink.decode_version2(bytes(invalid)) is None
    assert telink.decode_version2(telink.version_request()) is None


@pytest.mark.parametrize(
    ("effect", "expected"),
    [
        (telink.SystemEffect.OFF, "96000000000000000f87"),
        (telink.SystemEffect.CLUB_LIGHTS, "c90100000000142d0087"),
        (telink.SystemEffect.PAPARAZZI, "100100001430162d0187"),
        (telink.SystemEffect.FIREWORKS, "d70100000000142d0e87"),
        (telink.SystemEffect.FAULTY_BULB, "b801805201232d050887"),
        (telink.SystemEffect.LIGHTNING, "3a0100281530162d0287"),
        (telink.SystemEffect.TV, "cc0100000000142d0387"),
        (telink.SystemEffect.CANDLE, "cd0100000000142d0487"),
        (telink.SystemEffect.PULSING, "b901805201232d050987"),
        (telink.SystemEffect.STROBE, "3401005001232d050687"),
        (telink.SystemEffect.EXPLOSION, "2d01004801232d050787"),
        (telink.SystemEffect.FIRE, "ce0100000000142d0587"),
        (telink.SystemEffect.WELDING, "4f01402219002d150a87"),
        (telink.SystemEffect.COP_CAR, "540100000080142d0b87"),
        (telink.SystemEffect.COLOR_CHASE, "f80100000020172d0c87"),
        (telink.SystemEffect.PARTY_LIGHTS, "f90100000020172d0d87"),
    ],
)
def test_default_ace_effect_vectors(effect: telink.SystemEffect, expected: str) -> None:
    payload = telink.effect(
        effect,
        **(
            {"mode": 0}
            if effect
            in {
                telink.SystemEffect.FAULTY_BULB,
                telink.SystemEffect.PULSING,
                telink.SystemEffect.STROBE,
                telink.SystemEffect.EXPLOSION,
            }
            else {}
        ),
    )
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
                True,
                telink.SystemEffect.PAPARAZZI,
                900,
                11,
                kelvin=6500,
                gm=100,
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
                "mode": 0,
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
                gm=100,
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
                gm=100,
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
                "mode": 0,
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
                gm=100,
            ),
        ),
        (
            telink.SystemEffect.STROBE,
            {
                "intensity": 906,
                "frequency": 11,
                "trigger": 3,
                "kelvin": 6500,
                "mode": 0,
            },
            "1c010058a1a8e20b0687",
            telink.EffectState(
                True,
                telink.SystemEffect.STROBE,
                906,
                11,
                trigger=3,
                kelvin=6500,
                gm=100,
            ),
        ),
        (
            telink.SystemEffect.EXPLOSION,
            {
                "intensity": 907,
                "frequency": 10,
                "trigger": 0,
                "kelvin": 6500,
                "mode": 0,
            },
            "44010040a1e8e20a0787",
            telink.EffectState(
                True,
                telink.SystemEffect.EXPLOSION,
                907,
                10,
                trigger=0,
                kelvin=6500,
                gm=100,
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


@pytest.mark.parametrize(
    ("effect", "kwargs", "expected"),
    [
        (telink.SystemEffect.PAPARAZZI, {}, "160100001a30162d0187"),
        (telink.SystemEffect.LIGHTNING, {}, "400100281b30162d0287"),
        (telink.SystemEffect.WELDING, {"mode": 0}, "b90120b101232d050a87"),
        (telink.SystemEffect.FAULTY_BULB, {"mode": 0}, "180180b201232d050887"),
        (telink.SystemEffect.PULSING, {"mode": 0}, "190180b201232d050987"),
        (telink.SystemEffect.STROBE, {"mode": 0}, "940100b001232d050687"),
        (telink.SystemEffect.EXPLOSION, {"mode": 0}, "8d0100a801232d050787"),
    ],
)
def test_effect_gm_vectors_and_app_scale_round_trip(
    effect: telink.SystemEffect,
    kwargs: dict[str, int],
    expected: str,
) -> None:
    payload = telink.effect(effect, gm=135, **kwargs)
    assert payload.hex() == expected
    state = telink.decode_effect(payload)
    assert state is not None
    assert state.gm == 130
    assert state.gm_flag is False


@pytest.mark.parametrize(
    ("effect", "kwargs", "expected"),
    [
        (telink.SystemEffect.PAPARAZZI, {}, "c30100804730162d0187"),
        (telink.SystemEffect.LIGHTNING, {}, "7201002e4730162d0287"),
        (telink.SystemEffect.WELDING, {"mode": 0}, "88012c7104232d050a87"),
        (telink.SystemEffect.FAULTY_BULB, {"mode": 0}, "3b01e07204232d050887"),
        (telink.SystemEffect.PULSING, {"mode": 0}, "3c01e07204232d050987"),
        (telink.SystemEffect.STROBE, {"mode": 0}, "5d01007604232d050687"),
        (telink.SystemEffect.EXPLOSION, {"mode": 0}, "5601006e04232d050787"),
    ],
)
def test_effect_gm_v2_vectors_preserve_fine_values(
    effect: telink.SystemEffect,
    kwargs: dict[str, int],
    expected: str,
) -> None:
    """Match the APK's flag/high-bit G/M-v2 layouts for every CCT effect."""
    payload = telink.effect(effect, gm=135, gm_flag=1, **kwargs)

    assert payload.hex() == expected
    state = telink.decode_effect(bytes.fromhex(expected))
    assert state is not None
    assert state.gm == 135
    assert state.gm_flag is True


@pytest.mark.parametrize(("gm", "expected"), [(0, 0), (100, 100), (200, 200)])
def test_effect_gm_v2_boundaries(gm: int, expected: int) -> None:
    state = telink.decode_effect(
        telink.effect(telink.SystemEffect.PAPARAZZI, gm=gm, gm_flag=True)
    )

    assert state is not None
    assert state.gm == expected
    assert state.gm_flag is True


@pytest.mark.parametrize(
    ("gm", "expected"),
    [
        (-100, 0),
        (104.9, 100),
        (105, 100),
        (300, 200),
    ],
)
def test_effect_gm_clamps_and_matches_app_asymmetric_rounding(
    gm: float, expected: int
) -> None:
    state = telink.decode_effect(telink.effect(telink.SystemEffect.PAPARAZZI, gm=gm))
    assert state is not None
    assert state.gm == expected


@pytest.mark.parametrize(
    ("effect", "kwargs"),
    [
        (telink.SystemEffect.CLUB_LIGHTS, {}),
        (telink.SystemEffect.FIREWORKS, {}),
        (telink.SystemEffect.TV, {}),
        (telink.SystemEffect.CANDLE, {}),
        (telink.SystemEffect.FIRE, {}),
        (telink.SystemEffect.WELDING, {"mode": 1}),
        (telink.SystemEffect.COP_CAR, {}),
        (telink.SystemEffect.COLOR_CHASE, {}),
        (telink.SystemEffect.PARTY_LIGHTS, {}),
    ],
)
def test_effect_gm_is_absent_from_non_cct_layouts(
    effect: telink.SystemEffect, kwargs: dict[str, int]
) -> None:
    payload = telink.effect(effect, gm=0, **kwargs)
    assert payload == telink.effect(effect, gm=200, **kwargs)
    state = telink.decode_effect(payload)
    assert state is not None
    assert state.gm is None


@pytest.mark.parametrize(
    ("effect", "speed", "expected"),
    [
        (telink.SystemEffect.FAULTY_BULB, 9, "6d0100693268c2180887"),
        (telink.SystemEffect.PULSING, 9, "6e0100693268c2180987"),
        (telink.SystemEffect.STROBE, None, "620100603268c2180687"),
        (telink.SystemEffect.EXPLOSION, None, "630100603268c2180787"),
    ],
)
def test_legacy_hsi_effect_layouts_match_app_vectors(
    effect: telink.SystemEffect,
    speed: int | None,
    expected: str,
) -> None:
    """Exercise the full-colour mode built and parsed by the app's models."""
    payload = telink.effect(
        effect,
        intensity=777,
        frequency=8,
        speed=speed,
        trigger=2,
        mode=1,
        hue=321,
        saturation=73,
    )

    assert payload.hex() == expected
    state = telink.decode_effect(bytes.fromhex(expected))
    assert state == telink.EffectState(
        on=True,
        effect=effect,
        intensity=777,
        frequency=8,
        speed=0 if speed is None else speed,
        trigger=2,
        mode=1,
        hue=321,
        saturation=73,
    )


@pytest.mark.parametrize(
    "effect",
    [
        telink.SystemEffect.FAULTY_BULB,
        telink.SystemEffect.PULSING,
        telink.SystemEffect.STROBE,
        telink.SystemEffect.EXPLOSION,
    ],
)
def test_full_color_legacy_effects_default_to_app_hsi_mode(
    effect: telink.SystemEffect,
) -> None:
    assert telink.effect(effect) == telink.effect(effect, mode=1)
    state = telink.decode_effect(telink.effect(effect))
    assert state is not None
    assert state.mode == 1
    assert state.hue == 0
    assert state.saturation == 100


def test_legacy_effect_decoder_rejects_unmodeled_colour_modes() -> None:
    payload = bytearray(telink.effect(telink.SystemEffect.FAULTY_BULB, mode=1, hue=120))
    packet = int.from_bytes(payload, "little")
    packet &= ~(0xF << 60)
    packet |= 2 << 60
    payload = bytearray(packet.to_bytes(10, "little"))
    payload[0] = sum(payload[1:]) & 0xFF

    assert telink.decode_effect(bytes(payload)) is None


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


def test_effect_bounds_are_safe_for_full_protocol_range() -> None:
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
            mode=0,
        )
    )
    assert faulty == telink.EffectState(
        True,
        telink.SystemEffect.FAULTY_BULB,
        0,
        1,
        speed=10,
        trigger=3,
        kelvin=20000,
        gm=100,
    )


@pytest.mark.parametrize(
    "effect",
    [
        telink.SystemEffect.PAPARAZZI,
        telink.SystemEffect.LIGHTNING,
        telink.SystemEffect.FAULTY_BULB,
        telink.SystemEffect.PULSING,
        telink.SystemEffect.STROBE,
        telink.SystemEffect.EXPLOSION,
    ],
)
def test_effect_high_cct_round_trip(effect: telink.SystemEffect) -> None:
    state = telink.decode_effect(
        telink.effect(
            effect,
            kelvin=15000,
            **(
                {"mode": 0}
                if effect
                in {
                    telink.SystemEffect.FAULTY_BULB,
                    telink.SystemEffect.PULSING,
                    telink.SystemEffect.STROBE,
                    telink.SystemEffect.EXPLOSION,
                }
                else {}
            ),
        )
    )
    assert state is not None
    assert state.kelvin == 15000


def test_welding_cct_and_default_hsi_modes_round_trip() -> None:
    hsi_state = telink.decode_effect(telink.effect(telink.SystemEffect.WELDING))
    assert hsi_state == telink.EffectState(
        True,
        telink.SystemEffect.WELDING,
        180,
        5,
        speed=18,
        trigger=2,
        mode=1,
        hue=0,
        saturation=100,
    )

    cct_state = telink.decode_effect(
        telink.effect(
            telink.SystemEffect.WELDING,
            mode=0,
            speed=18,
            kelvin=15000,
        )
    )
    assert cct_state == telink.EffectState(
        True,
        telink.SystemEffect.WELDING,
        180,
        5,
        speed=18,
        trigger=2,
        kelvin=15000,
        gm=100,
    )


@pytest.mark.parametrize(
    ("effect", "variant"),
    [
        (telink.SystemEffect.CLUB_LIGHTS, 7),
        (telink.SystemEffect.CANDLE, 2),
        (telink.SystemEffect.COP_CAR, 4),
    ],
)
def test_additional_effect_variant_round_trip(
    effect: telink.SystemEffect, variant: int
) -> None:
    state = telink.decode_effect(
        telink.effect(effect, intensity=777, frequency=9, variant=variant)
    )
    assert state == telink.EffectState(
        True,
        effect,
        777,
        9,
        variant=variant,
    )


@pytest.mark.parametrize(
    ("effect", "saturation"),
    [
        (telink.SystemEffect.COLOR_CHASE, 73),
        (telink.SystemEffect.PARTY_LIGHTS, 64),
    ],
)
def test_additional_effect_saturation_round_trip(
    effect: telink.SystemEffect, saturation: int
) -> None:
    state = telink.decode_effect(
        telink.effect(effect, intensity=777, frequency=9, saturation=saturation)
    )
    assert state == telink.EffectState(
        True,
        effect,
        777,
        9,
        saturation=saturation,
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
        power_state_raw=True,
        battery_percent=85,
        runtime_minutes=300,
        battery_voltage_raw=14800,
        external_voltage_raw=20000,
    )

    current = bytes.fromhex("f80000779ba43800000a")
    state = telink.decode_power(current, protocol_version=42)
    assert state == telink.PowerState(
        source=telink.PowerSource.BATTERY,
        power_state_raw=False,
        battery_percent=77,
        runtime_minutes=3000,
        battery_voltage_raw=14500,
        external_voltage_raw=0,
    )
    assert telink.decode_report(current, protocol_version=42) == state


@pytest.mark.parametrize(
    ("payload", "expected_source", "expected_power_state_raw", "external_voltage"),
    [
        # Live-equivalent Ace 25x report: bit 20 is clear despite a 15 V input.
        (
            "7a000000c8b620983a0a",
            telink.PowerSource.EXTERNAL,
            False,
            15000,
        ),
        # Deliberately cross the fields in the other direction as well.
        ("b8001000c8b62000000a", telink.PowerSource.BATTERY, True, 0),
    ],
)
def test_decode_power_source_follows_external_voltage(
    payload: str,
    expected_source: telink.PowerSource,
    expected_power_state_raw: bool,
    external_voltage: int,
) -> None:
    """External voltage, not the independent bit-20 state, selects the source."""
    state = telink.decode_power(bytes.fromhex(payload), protocol_version=39)

    assert state == telink.PowerState(
        source=expected_source,
        power_state_raw=expected_power_state_raw,
        battery_percent=100,
        runtime_minutes=0,
        battery_voltage_raw=8374,
        external_voltage_raw=external_voltage,
    )


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


def test_status_decoder_rejects_overlong_payload() -> None:
    """A valid ten-byte state prefix cannot authenticate trailing bytes."""
    payload = telink.cct(4300, 640)
    assert telink.decode_status(payload) is not None
    assert telink.decode_status(payload + b"\x00") is None
