"""Golden vectors for command-34 SystemFX2 packets from the Sidus Link APK."""

from __future__ import annotations

from collections.abc import Mapping

import pytest
from amaranble import systemfx2

E = systemfx2.SystemEffect2


def _assert_checksum(payload: bytes) -> None:
    assert len(payload) == 10
    assert payload[0] == sum(payload[1:]) & 0xFF
    assert payload[9] == 0x80 | systemfx2.CMD_SYSTEM_EFFECT_2


def test_effect_ids_and_labels_are_stable() -> None:
    assert systemfx2.CMD_SYSTEM_EFFECT_2 == 34
    assert systemfx2.SYSTEM_EFFECT2_IDS == {
        E.PAPARAZZI_II: 0,
        E.LIGHTNING_II: 1,
        E.TV_II: 2,
        E.FIRE_II: 3,
        E.STROBE_II: 4,
        E.EXPLOSION_II: 5,
        E.FAULTY_BULB_II: 6,
        E.PULSING_II: 7,
        E.WELDING_II: 8,
        E.COP_CAR_II: 9,
        E.PARTY_LIGHTS_II: 10,
        E.FIREWORKS_II: 11,
        E.LIGHTNING_III: 12,
        E.TV_III: 13,
        E.FIRE_III: 14,
        E.FAULTY_BULB_III: 15,
        E.PULSING_III: 16,
        E.COP_CAR_III: 17,
    }
    assert len({effect.value for effect in E}) == 18
    assert all(effect.value.endswith((" II", " III")) for effect in E)
    assert frozenset(list(E)[:12]) == systemfx2.DEFAULTED_SYSTEM_EFFECT2
    assert frozenset(list(E)[12:]) == systemfx2.EXPLICIT_SYSTEM_EFFECT2


# Produced by the APK's Protocol2 classes after applying the corresponding
# *IIEffect model defaults. These are independent of the Python bit packer.
@pytest.mark.parametrize(
    ("effect", "expected"),
    [
        (
            E.PAPARAZZI_II,
            ("1f00000028f0a0c500a2", "79000000e090036400a2"),
        ),
        (E.LIGHTNING_II, ("be0070c80152454b01a2",)),
        (E.TV_II, ("7080430e402b454b02a2",)),
        (E.FIRE_II, ("7180430e402b454b03a2",)),
        (E.STROBE_II, ("f90000871c20454b04a2",)),
        (E.EXPLOSION_II, ("fa0000871c20454b05a2",)),
        (E.FAULTY_BULB_II, ("c30070c80152454b06a2",)),
        (E.PULSING_II, ("c40070c80152454b07a2",)),
        (
            E.WELDING_II,
            ("22000000a068a1cf08a2", "a10000000090036408a2"),
        ),
        (E.COP_CAR_II, ("bb0000000080454b09a2",)),
        (E.PARTY_LIGHTS_II, ("180000005090414b0aa2",)),
        (E.FIREWORKS_II, ("6900000050e0414b0ba2",)),
    ],
)
def test_generation_ii_app_default_vectors(
    effect: E,
    expected: tuple[str, ...],
) -> None:
    packets = systemfx2.effect2(effect)
    assert tuple(packet.hex() for packet in packets) == expected
    for packet in packets:
        _assert_checksum(packet)
        state = systemfx2.decode_effect2(packet)
        assert state is not None
        assert state.effect is effect


def test_default_decoded_parameters() -> None:
    lightning = systemfx2.decode_effect2(systemfx2.effect2(E.LIGHTNING_II)[0])
    assert lightning == systemfx2.SystemEffect2State(
        on=True,
        effect=E.LIGHTNING_II,
        state=1,
        intensity=180,
        frequency=5,
        speed=5,
        mode=1,
        hue=1,
        saturation=100,
        center_kelvin=5600,
    )

    tv = systemfx2.decode_effect2(systemfx2.effect2(E.TV_II)[0])
    assert tv == systemfx2.SystemEffect2State(
        on=True,
        effect=E.TV_II,
        state=1,
        intensity=180,
        speed=5,
        mode=1,
        min_hue=1,
        max_hue=180,
        saturation=100,
        center_kelvin=5600,
    )

    welding_packets = systemfx2.effect2(E.WELDING_II)
    welding = systemfx2.merge_effect2_states(
        systemfx2.decode_effect2(welding_packets[0]),
        systemfx2.decode_effect2(welding_packets[1]),  # type: ignore[arg-type]
    )
    assert welding == systemfx2.SystemEffect2State(
        on=True,
        effect=E.WELDING_II,
        state=1,
        intensity=500,
        frequency=5,
        mode=1,
        hue=1,
        saturation=100,
        center_kelvin=0,
        min_intensity=180,
    )


@pytest.mark.parametrize(
    ("effect", "options", "expected", "decoded_fields"),
    [
        (
            E.LIGHTNING_II,
            {
                "state": 1,
                "intensity": 777,
                "frequency": 12,
                "speed": 3,
                "mode": 0,
                "kelvin": 6150,
                "gm": 141,
            },
            "e700008d7b309c7001a2",
            {
                "intensity": 777,
                "frequency": 12,
                "speed": 3,
                "mode": 0,
                "kelvin": 6150,
                "gm": 141,
            },
        ),
        (
            E.TV_II,
            {
                "state": 1,
                "intensity": 702,
                "speed": 9,
                "mode": 0,
                "max_kelvin": 7200,
                "min_kelvin": 2750,
                "gm": 132,
            },
            "de0020bc0109e96b02a2",
            {
                "intensity": 702,
                "speed": 9,
                "mode": 0,
                "max_kelvin": 7200,
                "min_kelvin": 2750,
                "gm": 132,
            },
        ),
        (
            E.STROBE_II,
            {
                "state": 1,
                "intensity": 645,
                "speed": 11,
                "mode": 2,
                "gel_kelvin": 5950,
                "gel_origin": 1,
                "gel_type": 13,
                "color": 701,
            },
            "a500a0d77e475b6804a2",
            {
                "intensity": 645,
                "speed": 11,
                "mode": 2,
                "gel_kelvin": 5950,
                "gel_origin": 1,
                "gel_type": 13,
                "color": 701,
            },
        ),
        (
            E.FAULTY_BULB_II,
            {
                "state": 1,
                "intensity": 543,
                "speed": 10,
                "frequency": 3,
                "mode": 2,
                "gel_kelvin": 5450,
                "gel_origin": 1,
                "gel_type": 6,
                "color": 455,
            },
            "e5008eb36d34fa6106a2",
            {
                "intensity": 543,
                "speed": 10,
                "frequency": 3,
                "mode": 2,
                "gel_kelvin": 5450,
                "gel_origin": 1,
                "gel_type": 6,
                "color": 455,
            },
        ),
    ],
)
def test_generation_ii_explicit_java_vectors(
    effect: E,
    options: Mapping[str, int],
    expected: str,
    decoded_fields: Mapping[str, int],
) -> None:
    packet = systemfx2.effect2_packet(effect, **options)
    assert packet.hex() == expected
    decoded = systemfx2.decode_effect2(packet)
    assert decoded is not None
    for name, value in decoded_fields.items():
        assert getattr(decoded, name) == value


# Exact Protocol3 layouts. The app artifact has no matching effect-model
# defaults, so every field is supplied explicitly.
@pytest.mark.parametrize(
    ("effect", "options", "expected", "decoded_fields"),
    [
        (
            E.LIGHTNING_III,
            {
                "state": 3,
                "package_type": 0,
                "intensity": 777,
                "gap_time": 201,
                "min_gap_time": 37,
            },
            "3f0000004a244bd80ca2",
            {"package_type": 0, "intensity": 777, "gap_time": 201, "min_gap_time": 37},
        ),
        (
            E.LIGHTNING_III,
            {
                "state": 1,
                "package_type": 1,
                "mode": 2,
                "gel_kelvin": 6150,
                "gel_origin": 1,
                "gel_type": 9,
                "color": 777,
            },
            "cd000000249cf7680ca2",
            {
                "package_type": 1,
                "mode": 2,
                "gel_kelvin": 6150,
                "gel_origin": 1,
                "gel_type": 9,
                "color": 777,
            },
        ),
        (
            E.TV_III,
            {
                "state": 3,
                "package_type": 0,
                "intensity": 678,
                "gap_time": 234,
                "min_gap_time": 45,
            },
            "b90000005aa833d50da2",
            {"package_type": 0, "intensity": 678, "gap_time": 234, "min_gap_time": 45},
        ),
        (
            E.TV_III,
            {
                "state": 1,
                "package_type": 1,
                "mode": 1,
                "max_hue": 299,
                "min_hue": 37,
                "saturation": 88,
                "center_kelvin": 5350,
            },
            "ab00006bb02556660da2",
            {
                "package_type": 1,
                "mode": 1,
                "max_hue": 299,
                "min_hue": 37,
                "saturation": 88,
                "center_kelvin": 5350,
            },
        ),
        (
            E.FIRE_III,
            {
                "state": 3,
                "package_type": 0,
                "intensity": 579,
                "frequency": 173,
            },
            "0700000000681dd20ea2",
            {"package_type": 0, "intensity": 579, "frequency": 173},
        ),
        (
            E.FIRE_III,
            {
                "state": 1,
                "package_type": 1,
                "mode": 0,
                "max_kelvin": 7050,
                "min_kelvin": 2900,
                "gm": 123,
            },
            "e00000007b3a1a610ea2",
            {
                "package_type": 1,
                "mode": 0,
                "max_kelvin": 7050,
                "min_kelvin": 2900,
                "gm": 123,
            },
        ),
        (
            E.FAULTY_BULB_III,
            {
                "state": 3,
                "package_type": 0,
                "intensity": 481,
                "gap_time": 177,
                "min_gap_time": 31,
            },
            "8c0000003ec40acf0fa2",
            {"package_type": 0, "intensity": 481, "gap_time": 177, "min_gap_time": 31},
        ),
        (
            E.FAULTY_BULB_III,
            {
                "state": 0,
                "package_type": 1,
                "mode": 0,
                "kelvin": 4550,
                "gm": 118,
            },
            "7300000000ecb6200fa2",
            {"package_type": 1, "mode": 0, "kelvin": 4550, "gm": 118},
        ),
        (
            E.PULSING_III,
            {
                "state": 1,
                "intensity": 913,
                "frequency": 197,
                "mode": 2,
                "gel_kelvin": 5550,
                "gel_origin": 1,
                "gel_type": 7,
                "color": 612,
            },
            "8e00c8bc6f541c7910a2",
            {
                "intensity": 913,
                "frequency": 197,
                "mode": 2,
                "gel_kelvin": 5550,
                "gel_origin": 1,
                "gel_type": 7,
                "color": 612,
            },
        ),
        (
            E.COP_CAR_III,
            {"state": 1, "intensity": 814, "frequency": 13, "color": 6},
            "d200000000c0ed7211a2",
            {"intensity": 814, "frequency": 13, "color": 6},
        ),
    ],
)
def test_generation_iii_java_vectors(
    effect: E,
    options: Mapping[str, int],
    expected: str,
    decoded_fields: Mapping[str, int],
) -> None:
    packet = systemfx2.effect2_packet(effect, **options)
    assert packet.hex() == expected
    _assert_checksum(packet)
    decoded = systemfx2.decode_effect2(packet)
    assert decoded is not None
    assert decoded.effect is effect
    for name, value in decoded_fields.items():
        assert getattr(decoded, name) == value


@pytest.mark.parametrize("effect", list(E)[12:])
def test_generation_iii_defaults_are_not_guessed(effect: E) -> None:
    with pytest.raises(NotImplementedError, match="no proven app defaults"):
        systemfx2.effect2(effect)


def test_dual_page_packets_merge_in_either_order() -> None:
    package0 = systemfx2.effect2_packet(
        E.LIGHTNING_III,
        state=3,
        package_type=0,
        intensity=777,
        gap_time=201,
        min_gap_time=37,
    )
    package1 = systemfx2.effect2_packet(
        E.LIGHTNING_III,
        state=0,
        package_type=1,
        mode=2,
        gel_kelvin=6150,
        gel_origin=1,
        gel_type=9,
        color=777,
    )
    state0 = systemfx2.decode_effect2(package0)
    state1 = systemfx2.decode_effect2(package1)
    assert state0 is not None
    assert state1 is not None

    forward = systemfx2.merge_effect2_states(state0, state1)
    reverse = systemfx2.merge_effect2_states(state1, state0)
    assert forward == reverse
    assert forward == systemfx2.SystemEffect2State(
        on=False,
        effect=E.LIGHTNING_III,
        state=0,
        intensity=777,
        mode=2,
        gap_time=201,
        min_gap_time=37,
        color=777,
        gel_kelvin=6150,
        gel_origin=1,
        gel_type=9,
    )
    assert forward.active_fields == {
        "intensity",
        "mode",
        "gap_time",
        "min_gap_time",
        "color",
        "gel_kelvin",
        "gel_origin",
        "gel_type",
    }

    assert systemfx2.decode_report2(package1, state0) == forward


def test_merge_retains_active_state_when_configuration_page_updates() -> None:
    first, second = systemfx2.effect2(E.PAPARAZZI_II)
    merged = systemfx2.decode_report2(second, systemfx2.decode_report2(first))
    assert merged is not None
    assert merged.state == 1

    updated_configuration = systemfx2.effect2_packet(
        E.PAPARAZZI_II,
        state=3,
        package_type=0,
        intensity=900,
        gap_time=55,
        min_gap_time=12,
    )
    updated = systemfx2.decode_report2(updated_configuration, merged)
    assert updated is not None
    assert updated.state == 1
    assert updated.on is True
    assert updated.intensity == 900
    assert updated.hue == 1


def test_single_page_and_mismatched_effect_merge_use_current_state() -> None:
    lightning = systemfx2.decode_effect2(systemfx2.effect2(E.LIGHTNING_II)[0])
    tv = systemfx2.decode_effect2(systemfx2.effect2(E.TV_II)[0])
    assert lightning is not None
    assert tv is not None
    assert systemfx2.merge_effect2_states(lightning, tv) is tv
    assert systemfx2.merge_effect2_states(None, lightning) is lightning


def test_meaningful_field_metadata_distinguishes_layouts() -> None:
    assert systemfx2.system_effect2_fields(E.COP_CAR_III) == {
        "intensity",
        "frequency",
        "color",
    }
    assert "gel_type" in systemfx2.system_effect2_fields(E.PULSING_III)
    assert "gel_type" not in systemfx2.system_effect2_fields(E.LIGHTNING_II)
    assert "max_kelvin" in systemfx2.system_effect2_fields("TV III")
    assert E.TV_III in systemfx2.DUAL_PACKET_SYSTEM_EFFECT2
    assert E.PULSING_III not in systemfx2.DUAL_PACKET_SYSTEM_EFFECT2


def test_page_one_does_not_require_unused_intensity() -> None:
    packet = systemfx2.effect2_packet(
        E.PAPARAZZI_II,
        state=1,
        package_type=1,
        mode=0,
        kelvin=5600,
        gm=100,
    )
    state = systemfx2.decode_effect2(packet)
    assert state is not None
    assert state.intensity is None
    assert state.kelvin == 5600


def test_bounds_are_clamped_and_kelvin_is_truncated_to_50k_steps() -> None:
    packet = systemfx2.effect2_packet(
        E.COP_CAR_II,
        state=9,
        intensity=5000,
        frequency=99,
        color=99,
    )
    state = systemfx2.decode_effect2(packet)
    assert state is not None
    assert state.state == 3
    assert state.intensity == 1000
    assert state.frequency == 15
    assert state.color == 7

    cct_packet = systemfx2.effect2_packet(
        E.LIGHTNING_II,
        state=1,
        intensity=100,
        frequency=1,
        speed=1,
        mode=0,
        kelvin=5624,
        gm=100,
    )
    cct_state = systemfx2.decode_effect2(cct_packet)
    assert cct_state is not None
    assert cct_state.kelvin == 5600


def test_required_layout_fields_are_enforced() -> None:
    with pytest.raises(ValueError, match="intensity is required"):
        systemfx2.effect2_packet(
            E.COP_CAR_II,
            state=1,
            frequency=5,
            color=4,
        )
    with pytest.raises(ValueError, match="package_type is required"):
        systemfx2.effect2_packet(
            E.PAPARAZZI_II,
            state=1,
            intensity=180,
            gap_time=60,
            min_gap_time=20,
        )
    with pytest.raises(ValueError, match="mode is required"):
        systemfx2.effect2_packet(
            E.PAPARAZZI_II,
            state=1,
            package_type=1,
        )


def test_decoder_rejects_corrupt_foreign_and_unknown_payloads() -> None:
    valid = bytearray(systemfx2.effect2(E.COP_CAR_II)[0])

    corrupt = bytearray(valid)
    corrupt[3] ^= 1
    assert systemfx2.decode_effect2(bytes(corrupt)) is None
    assert systemfx2.decode_effect2(b"short") is None

    foreign = bytearray(valid)
    foreign[9] = 0x81
    foreign[0] = sum(foreign[1:]) & 0xFF
    assert systemfx2.decode_effect2(bytes(foreign)) is None

    unknown = bytearray(valid)
    unknown[8] = 0xFE
    unknown[0] = sum(unknown[1:]) & 0xFF
    assert systemfx2.decode_effect2(bytes(unknown)) is None
    assert systemfx2.decode_report2(bytes(unknown)) is None


def test_off_state_is_encoded_without_changing_configuration_page_state() -> None:
    single = systemfx2.decode_effect2(systemfx2.effect2(E.TV_II, on=False)[0])
    assert single is not None
    assert single.state == 0
    assert single.on is False

    configuration, active = systemfx2.effect2(E.PAPARAZZI_II, on=False)
    configuration_state = systemfx2.decode_effect2(configuration)
    active_state = systemfx2.decode_effect2(active)
    assert configuration_state is not None
    assert active_state is not None
    assert configuration_state.state == 3
    assert active_state.state == 0
