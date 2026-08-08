"""Golden vectors for the app's Telink command-33 pixel effects."""

from __future__ import annotations

import pytest
from amaranble import pixelfx


def _assert_packet(
    payload: bytes,
    expected_hex: str,
    expected_state: pixelfx.PixelEffectState,
) -> None:
    assert payload.hex() == expected_hex
    assert payload[0] == sum(payload[1:]) & 0xFF
    assert payload[9] == 0xA1
    assert pixelfx.decode(payload) == expected_state
    assert pixelfx.encode(expected_state) == payload


def test_command_effect_ids_and_labels_are_stable() -> None:
    assert pixelfx.CMD_PIXEL_EFFECT == 33
    assert pixelfx.PIXEL_EFFECT_IDS == {
        pixelfx.PixelEffect.COLOR_FADE: 0,
        pixelfx.PixelEffect.COLOR_CYCLE: 1,
        pixelfx.PixelEffect.ONE_PIXEL_CHASE: 2,
        pixelfx.PixelEffect.TWO_PIXEL_CHASE: 3,
        pixelfx.PixelEffect.THREE_PIXEL_CHASE: 4,
        pixelfx.PixelEffect.PIXEL_FIRE: 5,
        pixelfx.PixelEffect.RAINBOW: 7,
    }
    assert [effect.value for effect in pixelfx.PixelEffect] == [
        "Color Fade",
        "Color Cycle",
        "One Pixel Chase",
        "Two Pixel Chase",
        "Three Pixel Chase",
        "Pixel Fire",
        "Rainbow",
    ]


# Produced from the APK Pixel*Effect constructor defaults and buildProtocol
# packet order, independently of the values used by the low-level vector tests.
@pytest.mark.parametrize(
    ("effect", "expected"),
    [
        (
            pixelfx.PixelEffect.COLOR_FADE,
            (
                "4400002083032dd000a1",
                "3b008043c6132dd100a1",
                "c40000000090118200a1",
            ),
        ),
        (
            pixelfx.PixelEffect.COLOR_CYCLE,
            (
                "4500002083032dd001a1",
                "3c008043c6132dd101a1",
                "840000000050108201a1",
            ),
        ),
        (
            pixelfx.PixelEffect.ONE_PIXEL_CHASE,
            (
                "3c008043c6132dd002a1",
                "4700002083032dd102a1",
                "670000000020238102a1",
            ),
        ),
        (
            pixelfx.PixelEffect.TWO_PIXEL_CHASE,
            (
                "c100000000202dd003a1",
                "3e008043c6132dd103a1",
                "0300804386172dd203a1",
                "680000000020238103a1",
            ),
        ),
        (
            pixelfx.PixelEffect.THREE_PIXEL_CHASE,
            (
                "c200000000202dd004a1",
                "3f008043c6132dd104a1",
                "0400804386172dd204a1",
                "c9008043461b2dd304a1",
                "690000000020238104a1",
            ),
        ),
        (
            pixelfx.PixelEffect.PIXEL_FIRE,
            (
                "9500800c08b4d0d705a1",
                "a900003864b4d1e205a1",
                "760000000000508005a1",
            ),
        ),
        (pixelfx.PixelEffect.RAINBOW, ("a50000000032408b07a1",)),
    ],
)
def test_app_default_effect_sequences(
    effect: pixelfx.PixelEffect,
    expected: tuple[str, ...],
) -> None:
    packets = pixelfx.effect(effect)
    assert tuple(packet.hex() for packet in packets) == expected

    states = tuple(pixelfx.decode(packet) for packet in packets)
    assert all(state is not None and state.effect is effect for state in states)
    assert all(
        state.playback is pixelfx.PixelPlayback.CONTINUE
        for state in states[:-1]
        if state is not None
    )
    assert states[-1] is not None
    assert states[-1].playback is pixelfx.PixelPlayback.RUNNING
    assert tuple(pixelfx.encode(state) for state in states if state is not None) == (
        packets
    )


@pytest.mark.parametrize("effect", list(pixelfx.PixelEffect))
def test_app_default_effect_off_changes_only_final_playback(
    effect: pixelfx.PixelEffect,
) -> None:
    on_packets = pixelfx.effect(effect)
    off_packets = pixelfx.effect(effect, on=False)
    assert off_packets[:-1] == on_packets[:-1]

    final = pixelfx.decode(off_packets[-1])
    assert final is not None
    assert final.effect is effect
    assert final.playback is pixelfx.PixelPlayback.STOP


def test_app_default_effect_decoded_parameters() -> None:
    fade = tuple(pixelfx.decode(packet) for packet in pixelfx.effect("Color Fade"))
    assert fade[0] == pixelfx.PixelEffectState(
        pixelfx.PixelEffect.COLOR_FADE,
        pixelfx.PixelPlayback.CONTINUE,
        pixelfx.PixelPacketType.COLOR,
        serial=0,
        brightness=180,
        light_mode=pixelfx.PixelLightMode.CCT,
        cct_raw=112,
        gm_raw=100,
    )
    assert fade[1] == pixelfx.PixelEffectState(
        pixelfx.PixelEffect.COLOR_FADE,
        pixelfx.PixelPlayback.CONTINUE,
        pixelfx.PixelPacketType.COLOR,
        serial=1,
        brightness=180,
        light_mode=pixelfx.PixelLightMode.HSI,
        hue=120,
        saturation=100,
        hsi_cct_raw=112,
    )
    assert fade[2] == pixelfx.PixelEffectState(
        pixelfx.PixelEffect.COLOR_FADE,
        pixelfx.PixelPlayback.RUNNING,
        pixelfx.PixelPacketType.CONTROL,
        speed=100,
        direction=1,
        color_count=2,
    )

    fire = tuple(pixelfx.decode(packet) for packet in pixelfx.effect("Pixel Fire"))
    assert fire[0] is not None
    assert (
        fire[0].max_brightness,
        fire[0].min_brightness,
        fire[0].cct_raw,
        fire[0].gm_raw,
    ) == (500, 180, 64, 100)
    assert fire[1] is not None
    assert (
        fire[1].brightness,
        fire[1].hue,
        fire[1].saturation,
        fire[1].hsi_cct_raw,
    ) == (180, 360, 100, 112)
    assert fire[2] is not None
    assert (fire[2].frequency, fire[2].direction) == (20, 0)


@pytest.mark.parametrize(
    ("payload", "expected_hex", "expected_state"),
    [
        (
            pixelfx.color_fade(
                playback=pixelfx.PixelPlayback.RUNNING,
                color_count=3,
                direction=1,
                speed=500,
            ),
            "0b00000000d0178300a1",
            pixelfx.PixelEffectState(
                pixelfx.PixelEffect.COLOR_FADE,
                pixelfx.PixelPlayback.RUNNING,
                pixelfx.PixelPacketType.CONTROL,
                speed=500,
                direction=1,
                color_count=3,
            ),
        ),
        (
            pixelfx.color_cycle(
                playback=pixelfx.PixelPlayback.CONTINUE,
                color_count=4,
                direction=2,
                speed=501,
                change_way=1,
            ),
            "6200000000d527c401a1",
            pixelfx.PixelEffectState(
                pixelfx.PixelEffect.COLOR_CYCLE,
                pixelfx.PixelPlayback.CONTINUE,
                pixelfx.PixelPacketType.CONTROL,
                speed=501,
                direction=2,
                color_count=4,
                change_way=1,
            ),
        ),
        (
            pixelfx.chase(
                pixelfx.PixelEffect.ONE_PIXEL_CHASE,
                playback=pixelfx.PixelPlayback.RUNNING,
                group=0,
                direction=2,
                speed=502,
                pixel_length=1,
            ),
            "0400000000b02f8202a1",
            pixelfx.PixelEffectState(
                pixelfx.PixelEffect.ONE_PIXEL_CHASE,
                pixelfx.PixelPlayback.RUNNING,
                pixelfx.PixelPacketType.CONTROL,
                speed=502,
                direction=2,
                group=0,
                pixel_length=1,
            ),
        ),
        (
            pixelfx.chase(
                pixelfx.PixelEffect.TWO_PIXEL_CHASE,
                playback=pixelfx.PixelPlayback.PAUSE,
                group=1,
                direction=1,
                speed=503,
                pixel_length=2,
            ),
            "f000000000b84f4503a1",
            pixelfx.PixelEffectState(
                pixelfx.PixelEffect.TWO_PIXEL_CHASE,
                pixelfx.PixelPlayback.PAUSE,
                pixelfx.PixelPacketType.CONTROL,
                speed=503,
                direction=1,
                group=1,
                pixel_length=2,
            ),
        ),
        (
            pixelfx.chase(
                pixelfx.PixelEffect.THREE_PIXEL_CHASE,
                playback=pixelfx.PixelPlayback.CONTINUE,
                group=3,
                direction=3,
                speed=1023,
                pixel_length=7,
            ),
            "6b00000000f8ffcf04a1",
            pixelfx.PixelEffectState(
                pixelfx.PixelEffect.THREE_PIXEL_CHASE,
                pixelfx.PixelPlayback.CONTINUE,
                pixelfx.PixelPacketType.CONTROL,
                speed=1023,
                direction=3,
                group=3,
                pixel_length=7,
            ),
        ),
        (
            pixelfx.pixel_fire_control(
                playback=pixelfx.PixelPlayback.RUNNING,
                frequency=504,
                direction=1,
            ),
            "0e0000000000e18705a1",
            pixelfx.PixelEffectState(
                pixelfx.PixelEffect.PIXEL_FIRE,
                pixelfx.PixelPlayback.RUNNING,
                pixelfx.PixelPacketType.CONTROL,
                direction=1,
                frequency=504,
            ),
        ),
        (
            pixelfx.rainbow(
                playback=pixelfx.PixelPlayback.RUNNING,
                brightness=800,
                direction=1,
                speed=505,
            ),
            "d800000080fc02b207a1",
            pixelfx.PixelEffectState(
                pixelfx.PixelEffect.RAINBOW,
                pixelfx.PixelPlayback.RUNNING,
                None,
                speed=505,
                direction=1,
                brightness=800,
            ),
        ),
    ],
)
def test_control_packet_golden_vectors_and_round_trip(
    payload: bytes,
    expected_hex: str,
    expected_state: pixelfx.PixelEffectState,
) -> None:
    _assert_packet(payload, expected_hex, expected_state)


@pytest.mark.parametrize(
    ("payload", "expected_hex", "expected_state"),
    [
        (
            pixelfx.color(
                pixelfx.PixelEffect.COLOR_FADE,
                playback=pixelfx.PixelPlayback.RUNNING,
                serial=1,
                brightness=700,
                light_mode=pixelfx.PixelLightMode.CCT,
                cct_raw=320,
                gm_raw=100,
            ),
            "0e000020030aaf9100a1",
            pixelfx.PixelEffectState(
                pixelfx.PixelEffect.COLOR_FADE,
                pixelfx.PixelPlayback.RUNNING,
                pixelfx.PixelPacketType.COLOR,
                serial=1,
                brightness=700,
                light_mode=pixelfx.PixelLightMode.CCT,
                cct_raw=320,
                gm_raw=100,
            ),
        ),
        (
            pixelfx.color(
                pixelfx.PixelEffect.COLOR_CYCLE,
                playback=pixelfx.PixelPlayback.PAUSE,
                serial=2,
                brightness=701,
                light_mode=pixelfx.PixelLightMode.HSI,
                hue=315,
                saturation=73,
                hsi_cct_raw=280,
            ),
            "3000c098dc59af5201a1",
            pixelfx.PixelEffectState(
                pixelfx.PixelEffect.COLOR_CYCLE,
                pixelfx.PixelPlayback.PAUSE,
                pixelfx.PixelPacketType.COLOR,
                serial=2,
                brightness=701,
                light_mode=pixelfx.PixelLightMode.HSI,
                hue=315,
                saturation=73,
                hsi_cct_raw=280,
            ),
        ),
        (
            pixelfx.color(
                pixelfx.PixelEffect.ONE_PIXEL_CHASE,
                playback=pixelfx.PixelPlayback.CONTINUE,
                serial=3,
                brightness=702,
                light_mode=pixelfx.PixelLightMode.BLACK,
            ),
            "c500000000a0afd302a1",
            pixelfx.PixelEffectState(
                pixelfx.PixelEffect.ONE_PIXEL_CHASE,
                pixelfx.PixelPlayback.CONTINUE,
                pixelfx.PixelPacketType.COLOR,
                serial=3,
                brightness=702,
                light_mode=pixelfx.PixelLightMode.BLACK,
            ),
        ),
    ],
)
def test_common_color_packet_golden_vectors_and_round_trip(
    payload: bytes,
    expected_hex: str,
    expected_state: pixelfx.PixelEffectState,
) -> None:
    _assert_packet(payload, expected_hex, expected_state)


@pytest.mark.parametrize(
    ("payload", "expected_hex", "expected_state"),
    [
        (
            pixelfx.pixel_fire_color(
                playback=pixelfx.PixelPlayback.RUNNING,
                max_brightness=900,
                min_brightness=300,
                light_mode=pixelfx.PixelLightMode.CCT,
                cct_raw=320,
                gm_raw=100,
            ),
            "3500800c282c119e05a1",
            pixelfx.PixelEffectState(
                pixelfx.PixelEffect.PIXEL_FIRE,
                pixelfx.PixelPlayback.RUNNING,
                pixelfx.PixelPacketType.COLOR,
                max_brightness=900,
                min_brightness=300,
                light_mode=pixelfx.PixelLightMode.CCT,
                cct_raw=320,
                gm_raw=100,
            ),
        ),
        (
            pixelfx.pixel_fire_color(
                playback=pixelfx.PixelPlayback.PAUSE,
                max_brightness=901,
                min_brightness=301,
                light_mode=pixelfx.PixelLightMode.HSI,
                hue=315,
                saturation=73,
                hsi_cct_raw=280,
            ),
            "82006372672d155e05a1",
            pixelfx.PixelEffectState(
                pixelfx.PixelEffect.PIXEL_FIRE,
                pixelfx.PixelPlayback.PAUSE,
                pixelfx.PixelPacketType.COLOR,
                max_brightness=901,
                min_brightness=301,
                light_mode=pixelfx.PixelLightMode.HSI,
                hue=315,
                saturation=73,
                hsi_cct_raw=280,
            ),
        ),
        (
            pixelfx.pixel_fire_color(
                playback=pixelfx.PixelPlayback.CONTINUE,
                max_brightness=902,
                min_brightness=302,
                light_mode=pixelfx.PixelLightMode.BLACK,
            ),
            "4b000000802e19de05a1",
            pixelfx.PixelEffectState(
                pixelfx.PixelEffect.PIXEL_FIRE,
                pixelfx.PixelPlayback.CONTINUE,
                pixelfx.PixelPacketType.COLOR,
                max_brightness=902,
                min_brightness=302,
                light_mode=pixelfx.PixelLightMode.BLACK,
            ),
        ),
        (
            pixelfx.pixel_fire_base(
                playback=pixelfx.PixelPlayback.RUNNING,
                brightness=600,
                light_mode=pixelfx.PixelLightMode.CCT,
                cct_raw=320,
                gm_raw=100,
            ),
            "8100000032a060a905a1",
            pixelfx.PixelEffectState(
                pixelfx.PixelEffect.PIXEL_FIRE,
                pixelfx.PixelPlayback.RUNNING,
                pixelfx.PixelPacketType.BASE,
                brightness=600,
                light_mode=pixelfx.PixelLightMode.CCT,
                cct_raw=320,
                gm_raw=100,
            ),
        ),
        (
            pixelfx.pixel_fire_base(
                playback=pixelfx.PixelPlayback.PAUSE,
                brightness=601,
                light_mode=pixelfx.PixelLightMode.HSI,
                hue=315,
                saturation=73,
                hsi_cct_raw=280,
            ),
            "6600008cc99d656905a1",
            pixelfx.PixelEffectState(
                pixelfx.PixelEffect.PIXEL_FIRE,
                pixelfx.PixelPlayback.PAUSE,
                pixelfx.PixelPacketType.BASE,
                brightness=601,
                light_mode=pixelfx.PixelLightMode.HSI,
                hue=315,
                saturation=73,
                hsi_cct_raw=280,
            ),
        ),
        (
            pixelfx.pixel_fire_base(
                playback=pixelfx.PixelPlayback.CONTINUE,
                brightness=602,
                light_mode=pixelfx.PixelLightMode.BLACK,
            ),
            "f900000000006ae905a1",
            pixelfx.PixelEffectState(
                pixelfx.PixelEffect.PIXEL_FIRE,
                pixelfx.PixelPlayback.CONTINUE,
                pixelfx.PixelPacketType.BASE,
                brightness=602,
                light_mode=pixelfx.PixelLightMode.BLACK,
            ),
        ),
    ],
)
def test_pixel_fire_color_packet_golden_vectors_and_round_trip(
    payload: bytes,
    expected_hex: str,
    expected_state: pixelfx.PixelEffectState,
) -> None:
    _assert_packet(payload, expected_hex, expected_state)


def _as_report(command: bytes) -> bytes:
    payload = bytearray(command)
    payload[9] &= 0x7F
    payload[0] = sum(payload[1:]) & 0xFF
    return bytes(payload)


def test_command_and_report_decode_identically() -> None:
    command = pixelfx.color(
        pixelfx.PixelEffect.THREE_PIXEL_CHASE,
        playback=pixelfx.PixelPlayback.RUNNING,
        serial=7,
        brightness=777,
        light_mode=pixelfx.PixelLightMode.HSI,
        hue=270,
        saturation=80,
        hsi_cct_raw=250,
    )
    report = _as_report(command)
    assert report[9] == pixelfx.CMD_PIXEL_EFFECT
    state = pixelfx.decode(report)
    assert state == pixelfx.decode(command)
    assert state is not None
    assert pixelfx.encode(state) == command
    assert pixelfx.encode(state) != report


@pytest.mark.parametrize("playback", list(pixelfx.PixelPlayback))
def test_all_app_declared_playback_states_round_trip(
    playback: pixelfx.PixelPlayback,
) -> None:
    state = pixelfx.decode(
        pixelfx.color_fade(
            playback=playback,
            color_count=1,
            direction=0,
            speed=1,
        )
    )
    assert state is not None
    assert state.playback is playback


def test_wire_width_bounds_and_javascript_rounding() -> None:
    state = pixelfx.decode(
        pixelfx.color_cycle(
            playback=pixelfx.PixelPlayback.RUNNING,
            color_count=-1,
            direction=99,
            speed=1024,
            change_way=2.5,
        )
    )
    assert state is not None
    assert state.color_count == 0
    assert state.direction == 15
    assert state.speed == 1023
    assert state.change_way == 3

    rounded = pixelfx.decode(
        pixelfx.rainbow(
            playback=pixelfx.PixelPlayback.RUNNING,
            brightness=500.5,
            direction=0.5,
            speed=100.5,
        )
    )
    assert rounded is not None
    assert (rounded.brightness, rounded.direction, rounded.speed) == (501, 1, 101)

    nonfinite = pixelfx.decode(
        pixelfx.rainbow(
            playback=pixelfx.PixelPlayback.RUNNING,
            brightness=float("inf"),
            direction=float("nan"),
            speed=float("-inf"),
        )
    )
    assert nonfinite is not None
    assert (nonfinite.brightness, nonfinite.direction, nonfinite.speed) == (
        1023,
        0,
        0,
    )


def test_color_fields_clamp_to_their_proven_wire_widths() -> None:
    cct = pixelfx.decode(
        pixelfx.color(
            pixelfx.PixelEffect.COLOR_FADE,
            playback=pixelfx.PixelPlayback.RUNNING,
            serial=99,
            brightness=9999,
            light_mode=pixelfx.PixelLightMode.CCT,
            cct_raw=9999,
            gm_raw=9999,
        )
    )
    assert cct is not None
    assert (cct.serial, cct.brightness, cct.cct_raw, cct.gm_raw) == (
        15,
        1023,
        511,
        255,
    )

    hsi = pixelfx.decode(
        pixelfx.pixel_fire_base(
            playback=pixelfx.PixelPlayback.RUNNING,
            brightness=9999,
            light_mode=pixelfx.PixelLightMode.HSI,
            hue=9999,
            saturation=9999,
            hsi_cct_raw=9999,
        )
    )
    assert hsi is not None
    assert (hsi.hue, hsi.saturation, hsi.hsi_cct_raw) == (511, 127, 511)


def test_builder_rejects_invalid_layouts_and_missing_color_fields() -> None:
    with pytest.raises(ValueError, match="not a chase effect"):
        pixelfx.chase(
            pixelfx.PixelEffect.RAINBOW,
            playback=pixelfx.PixelPlayback.RUNNING,
            group=0,
            direction=0,
            speed=0,
            pixel_length=0,
        )
    with pytest.raises(ValueError, match="does not use common color packets"):
        pixelfx.color(
            pixelfx.PixelEffect.PIXEL_FIRE,
            playback=pixelfx.PixelPlayback.RUNNING,
            serial=0,
            brightness=0,
            light_mode=pixelfx.PixelLightMode.BLACK,
        )
    with pytest.raises(ValueError, match="require cct_raw and gm_raw"):
        pixelfx.color(
            pixelfx.PixelEffect.COLOR_FADE,
            playback=pixelfx.PixelPlayback.RUNNING,
            serial=0,
            brightness=0,
            light_mode=pixelfx.PixelLightMode.CCT,
        )
    with pytest.raises(ValueError, match="require hue, saturation"):
        pixelfx.pixel_fire_color(
            playback=pixelfx.PixelPlayback.RUNNING,
            max_brightness=0,
            min_brightness=0,
            light_mode=pixelfx.PixelLightMode.HSI,
        )


def _replace_bits(payload: bytes, value: int, start: int, width: int) -> bytes:
    packet = int.from_bytes(payload, "little")
    mask = ((1 << width) - 1) << start
    packet = (packet & ~mask) | ((value << start) & mask)
    result = bytearray(packet.to_bytes(10, "little"))
    result[0] = sum(result[1:]) & 0xFF
    return bytes(result)


def test_decode_rejects_invalid_or_unsupported_packets() -> None:
    valid = pixelfx.color_fade(
        playback=pixelfx.PixelPlayback.RUNNING,
        color_count=3,
        direction=1,
        speed=500,
    )
    invalid_checksum = bytearray(valid)
    invalid_checksum[1] ^= 1
    assert pixelfx.decode(bytes(invalid_checksum)) is None
    assert pixelfx.decode(b"short") is None

    unknown_effect_six = _replace_bits(valid, 6, 64, 8)
    assert pixelfx.decode(unknown_effect_six) is None

    noncanonical_base_packet = _replace_bits(valid, pixelfx.PixelPacketType.BASE, 60, 2)
    assert pixelfx.decode(noncanonical_base_packet) is None

    invalid_light_mode = pixelfx.color(
        pixelfx.PixelEffect.COLOR_FADE,
        playback=pixelfx.PixelPlayback.RUNNING,
        serial=0,
        brightness=0,
        light_mode=pixelfx.PixelLightMode.BLACK,
    )
    invalid_light_mode = _replace_bits(invalid_light_mode, 3, 44, 2)
    assert pixelfx.decode(invalid_light_mode) is None
