"""Golden-vector and validation tests for raw Manual FX command 31."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from amaranble import manualfx

type Builder = Callable[..., bytes]


def _assert_round_trip(
    payload: bytes,
    expected_hex: str,
    expected_message: manualfx.ManualFxMessage,
) -> None:
    assert payload.hex() == expected_hex
    assert payload[0] == sum(payload[1:]) & 0xFF
    assert manualfx.decode(payload) == expected_message
    assert manualfx.encode(expected_message) == payload


def test_command_subtype_and_option_values_are_exact() -> None:
    assert manualfx.CMD_MANUAL_EFFECT == 31
    assert list(manualfx.ManualFxSubtype) == [
        manualfx.ManualFxSubtype.COLOR_AND_INTENSITY,
        manualfx.ManualFxSubtype.CYCLE_AND_LOOP,
        manualfx.ManualFxSubtype.FREQUENCY_AND_TIMING,
        manualfx.ManualFxSubtype.FADE_TIMING,
    ]
    assert [int(value) for value in manualfx.ManualFxSubtype] == [0, 1, 2, 3]
    assert int(manualfx.ManualFxOption.ACK_OR_READ) == 0
    assert int(manualfx.ManualFxOption.WRITE) == 1


_SUBTYPE0_CCT = {
    "base_raw": 0,
    "ctrl_raw": 2,
    "intensity_seq_raw": 3,
    "intensity_max_raw": 101,
    "intensity_min_raw": 37,
    "gm_seq_raw": 1,
    "gm_max_raw": 99,
    "gm_min_raw": 17,
    "cct_seq_raw": 2,
    "cct_max_raw": 777,
    "cct_min_raw": 333,
}

_SUBTYPE0_HSI = {
    "base_raw": 3,
    "ctrl_raw": 1,
    "intensity_seq_raw": 2,
    "intensity_max_raw": 88,
    "intensity_min_raw": 12,
    "saturation_seq_raw": 3,
    "saturation_max_raw": 100,
    "saturation_min_raw": 15,
    "hue_seq_raw": 1,
    "hue_max_raw": 359,
    "hue_min_raw": 27,
    "option": manualfx.ManualFxOption.ACK_OR_READ,
}

_SUBTYPE1_MODE0 = {
    "effect_mode_raw": 0,
    "ctrl_raw": 1,
    "cycle_time_seq_raw": 2,
    "cycle_time_max_raw": 800,
    "cycle_time_min_raw": 123,
    "loop_times_raw": 19,
    "loop_mode_raw": 2,
    "free_time_seq_raw": 3,
    "free_time_max_raw": 511,
    "free_time_min_raw": 17,
}

_SUBTYPE1_MODE1 = {
    "effect_mode_raw": 1,
    "ctrl_raw": 2,
    "cycle_time_seq_raw": 1,
    "cycle_time_max_raw": 700,
    "cycle_time_min_raw": 222,
    "loop_times_raw": 31,
    "loop_mode_raw": 1,
    "fade_in_curve_raw": 3,
    "fade_in_time_seq_raw": 2,
    "fade_in_time_max_raw": 456,
    "fade_in_time_min_raw": 78,
}

_SUBTYPE1_MODE2 = {
    "effect_mode_raw": 2,
    "ctrl_raw": 3,
    "cycle_time_seq_raw": 3,
    "cycle_time_max_raw": 900,
    "cycle_time_min_raw": 300,
    "loop_times_raw": 64,
    "loop_mode_raw": 3,
    "unit_time_seg_raw": 1,
    "free_time_seg_raw": 2,
    "overlap_seq_raw": 3,
    "olr_seq_raw": 1,
    "olr_max_raw": 100,
    "olr_min_raw": 21,
}

_SUBTYPE1_MODE3 = {
    "effect_mode_raw": 3,
    "ctrl_raw": 0,
    "cycle_time_seq_raw": 0,
    "cycle_time_max_raw": 640,
    "cycle_time_min_raw": 1,
    "loop_times_raw": 127,
    "loop_mode_raw": 0,
    "unit_time_seg_raw": 3,
    "free_time_seg_raw": 0,
    "overlap_seq_raw": 2,
    "olr_seq_raw": 3,
    "olr_max_raw": 127,
    "olr_min_raw": 0,
    "option": manualfx.ManualFxOption.ACK_OR_READ,
}

_SUBTYPE2_MODE0 = {
    "effect_mode_raw": 0,
    "ctrl_raw": 2,
    "frequency_seq_raw": 1,
    "frequency_max_raw": 240,
    "frequency_min_raw": 7,
    "unit_time_seq_raw": 3,
    "unit_time_max_raw": 999,
    "unit_time_min_raw": 123,
}

_SUBTYPE2_MODE1 = {
    "effect_mode_raw": 1,
    "ctrl_raw": 1,
    "flicker_frequency_raw": 77,
    "fade_out_curve_raw": 2,
    "fade_out_time_seq_raw": 1,
    "fade_out_time_max_raw": 700,
    "fade_out_time_min_raw": 44,
}

_SUBTYPE2_MODE2 = {
    "effect_mode_raw": 2,
    "ctrl_raw": 3,
    "olp_seq_raw": 2,
    "olp_max_raw": 99,
    "olp_min_raw": 17,
    "unit_time_max_raw": 800,
    "unit_time_min_raw": 200,
    "free_time_max_raw": 511,
    "free_time_min_raw": 12,
}

_SUBTYPE2_MODE3 = {
    "effect_mode_raw": 3,
    "ctrl_raw": 0,
    "olp_seq_raw": 3,
    "olp_max_raw": 127,
    "olp_min_raw": 0,
    "unit_time_max_raw": 1023,
    "unit_time_min_raw": 1,
    "free_time_max_raw": 777,
    "free_time_min_raw": 333,
    "option": manualfx.ManualFxOption.ACK_OR_READ,
}

_SUBTYPE3 = {
    "ctrl_raw": 2,
    "flicker_frequency_raw": 199,
    "fade_out_curve_raw": 3,
    "fade_out_time_seq_raw": 1,
    "fade_out_time_max_raw": 888,
    "fade_out_time_min_raw": 222,
    "fade_in_curve_raw": 2,
    "fade_in_time_seq_raw": 3,
    "fade_in_time_max_raw": 777,
    "fade_in_time_min_raw": 111,
}


# These bytes were calculated directly from the Java field offsets, outside
# the production codec. Together they exercise every subtype and effect-mode
# branch, both command-option values, and values spanning byte boundaries.
@pytest.mark.parametrize(
    ("builder", "kwargs", "expected_hex", "expected_message"),
    [
        (
            manualfx.build_subtype0,
            _SUBTYPE0_CCT,
            "c4d03862c24d5d2e219f",
            manualfx.ManualFxSubtype0(**_SUBTYPE0_CCT),
        ),
        (
            manualfx.build_subtype0,
            _SUBTYPE0_HSI,
            "9630f9d1591b88651c1f",
            manualfx.ManualFxSubtype0(**_SUBTYPE0_HSI),
        ),
        (
            manualfx.build_subtype1,
            _SUBTYPE1_MODE0,
            "49e0ff2210e43d26529f",
            manualfx.ManualFxSubtype1(**_SUBTYPE1_MODE0),
        ),
        (
            manualfx.build_subtype1,
            _SUBTYPE1_MODE1,
            "6858e49c88576f3e659f",
            manualfx.ManualFxSubtype1(**_SUBTYPE1_MODE1),
        ),
        (
            manualfx.build_subtype1,
            _SUBTYPE1_MODE2,
            "44208f5c997096807b9f",
            manualfx.ManualFxSubtype1(**_SUBTYPE1_MODE2),
        ),
        (
            manualfx.build_subtype1,
            _SUBTYPE1_MODE3,
            "a460fc0f00d000fe4c1f",
            manualfx.ManualFxSubtype1(**_SUBTYPE1_MODE3),
        ),
        (
            manualfx.build_subtype2,
            _SUBTYPE2_MODE0,
            "260000047ff0f97ba09f",
            manualfx.ManualFxSubtype2(**_SUBTYPE2_MODE0),
        ),
        (
            manualfx.build_subtype2,
            _SUBTYPE2_MODE1,
            "5b0000003419af2c949f",
            manualfx.ManualFxSubtype2(**_SUBTYPE2_MODE1),
        ),
        (
            manualfx.build_subtype2,
            _SUBTYPE2_MODE2,
            "80388e808ccc7f0cb89f",
            manualfx.ManualFxSubtype2(**_SUBTYPE2_MODE2),
        ),
        (
            manualfx.build_subtype2,
            _SUBTYPE2_MODE3,
            "19fc07fc1f40c24d8d1f",
            manualfx.ManualFxSubtype2(**_SUBTYPE2_MODE3),
        ),
        (
            manualfx.build_subtype3,
            _SUBTYPE3,
            "271c1fdede78c26fe89f",
            manualfx.ManualFxSubtype3(**_SUBTYPE3),
        ),
    ],
    ids=[
        "subtype0-cct",
        "subtype0-hsi",
        "subtype1-mode0",
        "subtype1-mode1",
        "subtype1-mode2",
        "subtype1-mode3",
        "subtype2-mode0",
        "subtype2-mode1",
        "subtype2-mode2",
        "subtype2-mode3",
        "subtype3",
    ],
)
def test_manual_fx_golden_vectors_and_round_trip(
    builder: Builder,
    kwargs: dict[str, Any],
    expected_hex: str,
    expected_message: manualfx.ManualFxMessage,
) -> None:
    _assert_round_trip(builder(**kwargs), expected_hex, expected_message)


def test_dedicated_decoders_reject_other_subtypes() -> None:
    payloads = [
        manualfx.build_subtype0(**_SUBTYPE0_CCT),
        manualfx.build_subtype1(**_SUBTYPE1_MODE0),
        manualfx.build_subtype2(**_SUBTYPE2_MODE0),
        manualfx.build_subtype3(**_SUBTYPE3),
    ]
    decoders = [
        manualfx.decode_subtype0,
        manualfx.decode_subtype1,
        manualfx.decode_subtype2,
        manualfx.decode_subtype3,
    ]
    for expected_index, decoder in enumerate(decoders):
        for payload_index, payload in enumerate(payloads):
            if payload_index == expected_index:
                assert decoder(payload) == manualfx.decode(payload)
            else:
                assert decoder(payload) is None


_WIDTH_CASES: list[tuple[Builder, dict[str, Any], str, int]] = [
    # Subtype 0: both mutually exclusive color layouts and common fields.
    (manualfx.build_subtype0, _SUBTYPE0_CCT, "base_raw", 3),
    (manualfx.build_subtype0, _SUBTYPE0_CCT, "ctrl_raw", 3),
    (manualfx.build_subtype0, _SUBTYPE0_CCT, "intensity_seq_raw", 3),
    (manualfx.build_subtype0, _SUBTYPE0_CCT, "intensity_max_raw", 127),
    (manualfx.build_subtype0, _SUBTYPE0_CCT, "intensity_min_raw", 127),
    (manualfx.build_subtype0, _SUBTYPE0_CCT, "gm_seq_raw", 3),
    (manualfx.build_subtype0, _SUBTYPE0_CCT, "gm_max_raw", 127),
    (manualfx.build_subtype0, _SUBTYPE0_CCT, "gm_min_raw", 127),
    (manualfx.build_subtype0, _SUBTYPE0_CCT, "cct_seq_raw", 3),
    (manualfx.build_subtype0, _SUBTYPE0_CCT, "cct_max_raw", 1023),
    (manualfx.build_subtype0, _SUBTYPE0_CCT, "cct_min_raw", 1023),
    (manualfx.build_subtype0, _SUBTYPE0_HSI, "saturation_seq_raw", 3),
    (manualfx.build_subtype0, _SUBTYPE0_HSI, "saturation_max_raw", 127),
    (manualfx.build_subtype0, _SUBTYPE0_HSI, "saturation_min_raw", 127),
    (manualfx.build_subtype0, _SUBTYPE0_HSI, "hue_seq_raw", 3),
    (manualfx.build_subtype0, _SUBTYPE0_HSI, "hue_max_raw", 1023),
    (manualfx.build_subtype0, _SUBTYPE0_HSI, "hue_min_raw", 1023),
    # Subtype 1: common fields and all three mode-specific layouts.
    (manualfx.build_subtype1, _SUBTYPE1_MODE0, "effect_mode_raw", 3),
    (manualfx.build_subtype1, _SUBTYPE1_MODE0, "ctrl_raw", 3),
    (manualfx.build_subtype1, _SUBTYPE1_MODE0, "cycle_time_seq_raw", 3),
    (manualfx.build_subtype1, _SUBTYPE1_MODE0, "cycle_time_max_raw", 1023),
    (manualfx.build_subtype1, _SUBTYPE1_MODE0, "cycle_time_min_raw", 1023),
    (manualfx.build_subtype1, _SUBTYPE1_MODE0, "loop_times_raw", 127),
    (manualfx.build_subtype1, _SUBTYPE1_MODE0, "loop_mode_raw", 3),
    (manualfx.build_subtype1, _SUBTYPE1_MODE0, "free_time_seq_raw", 3),
    (manualfx.build_subtype1, _SUBTYPE1_MODE0, "free_time_max_raw", 1023),
    (manualfx.build_subtype1, _SUBTYPE1_MODE0, "free_time_min_raw", 1023),
    (manualfx.build_subtype1, _SUBTYPE1_MODE1, "fade_in_curve_raw", 3),
    (manualfx.build_subtype1, _SUBTYPE1_MODE1, "fade_in_time_seq_raw", 3),
    (manualfx.build_subtype1, _SUBTYPE1_MODE1, "fade_in_time_max_raw", 1023),
    (manualfx.build_subtype1, _SUBTYPE1_MODE1, "fade_in_time_min_raw", 1023),
    (manualfx.build_subtype1, _SUBTYPE1_MODE2, "unit_time_seg_raw", 3),
    (manualfx.build_subtype1, _SUBTYPE1_MODE2, "free_time_seg_raw", 3),
    (manualfx.build_subtype1, _SUBTYPE1_MODE2, "overlap_seq_raw", 3),
    (manualfx.build_subtype1, _SUBTYPE1_MODE2, "olr_seq_raw", 3),
    (manualfx.build_subtype1, _SUBTYPE1_MODE2, "olr_max_raw", 127),
    (manualfx.build_subtype1, _SUBTYPE1_MODE2, "olr_min_raw", 127),
    # Subtype 2: common fields and all three mode-specific layouts.
    (manualfx.build_subtype2, _SUBTYPE2_MODE0, "effect_mode_raw", 3),
    (manualfx.build_subtype2, _SUBTYPE2_MODE0, "ctrl_raw", 3),
    (manualfx.build_subtype2, _SUBTYPE2_MODE0, "frequency_seq_raw", 3),
    (manualfx.build_subtype2, _SUBTYPE2_MODE0, "frequency_max_raw", 255),
    (manualfx.build_subtype2, _SUBTYPE2_MODE0, "frequency_min_raw", 255),
    (manualfx.build_subtype2, _SUBTYPE2_MODE0, "unit_time_seq_raw", 3),
    (manualfx.build_subtype2, _SUBTYPE2_MODE0, "unit_time_max_raw", 1023),
    (manualfx.build_subtype2, _SUBTYPE2_MODE0, "unit_time_min_raw", 1023),
    (manualfx.build_subtype2, _SUBTYPE2_MODE1, "flicker_frequency_raw", 255),
    (manualfx.build_subtype2, _SUBTYPE2_MODE1, "fade_out_curve_raw", 3),
    (manualfx.build_subtype2, _SUBTYPE2_MODE1, "fade_out_time_seq_raw", 3),
    (manualfx.build_subtype2, _SUBTYPE2_MODE1, "fade_out_time_max_raw", 1023),
    (manualfx.build_subtype2, _SUBTYPE2_MODE1, "fade_out_time_min_raw", 1023),
    (manualfx.build_subtype2, _SUBTYPE2_MODE2, "olp_seq_raw", 3),
    (manualfx.build_subtype2, _SUBTYPE2_MODE2, "olp_max_raw", 127),
    (manualfx.build_subtype2, _SUBTYPE2_MODE2, "olp_min_raw", 127),
    (manualfx.build_subtype2, _SUBTYPE2_MODE2, "unit_time_max_raw", 1023),
    (manualfx.build_subtype2, _SUBTYPE2_MODE2, "unit_time_min_raw", 1023),
    (manualfx.build_subtype2, _SUBTYPE2_MODE2, "free_time_max_raw", 1023),
    (manualfx.build_subtype2, _SUBTYPE2_MODE2, "free_time_min_raw", 1023),
    # Subtype 3's fixed layout.
    (manualfx.build_subtype3, _SUBTYPE3, "ctrl_raw", 3),
    (manualfx.build_subtype3, _SUBTYPE3, "flicker_frequency_raw", 255),
    (manualfx.build_subtype3, _SUBTYPE3, "fade_out_curve_raw", 3),
    (manualfx.build_subtype3, _SUBTYPE3, "fade_out_time_seq_raw", 3),
    (manualfx.build_subtype3, _SUBTYPE3, "fade_out_time_max_raw", 1023),
    (manualfx.build_subtype3, _SUBTYPE3, "fade_out_time_min_raw", 1023),
    (manualfx.build_subtype3, _SUBTYPE3, "fade_in_curve_raw", 3),
    (manualfx.build_subtype3, _SUBTYPE3, "fade_in_time_seq_raw", 3),
    (manualfx.build_subtype3, _SUBTYPE3, "fade_in_time_max_raw", 1023),
    (manualfx.build_subtype3, _SUBTYPE3, "fade_in_time_min_raw", 1023),
]


@pytest.mark.parametrize(
    ("builder", "kwargs", "field", "maximum"),
    _WIDTH_CASES,
    ids=[case[2] for case in _WIDTH_CASES],
)
def test_every_raw_field_rejects_values_outside_its_wire_width(
    builder: Builder,
    kwargs: dict[str, Any],
    field: str,
    maximum: int,
) -> None:
    for invalid in (-1, maximum + 1):
        with pytest.raises(ValueError, match=field):
            builder(**(kwargs | {field: invalid}))


@pytest.mark.parametrize(
    ("builder", "kwargs", "field"),
    [
        (manualfx.build_subtype0, _SUBTYPE0_CCT, "gm_seq_raw"),
        (manualfx.build_subtype0, _SUBTYPE0_HSI, "hue_max_raw"),
        (manualfx.build_subtype1, _SUBTYPE1_MODE0, "free_time_seq_raw"),
        (manualfx.build_subtype1, _SUBTYPE1_MODE1, "fade_in_curve_raw"),
        (manualfx.build_subtype1, _SUBTYPE1_MODE2, "olr_min_raw"),
        (manualfx.build_subtype2, _SUBTYPE2_MODE0, "frequency_seq_raw"),
        (manualfx.build_subtype2, _SUBTYPE2_MODE1, "fade_out_curve_raw"),
        (manualfx.build_subtype2, _SUBTYPE2_MODE2, "olp_seq_raw"),
    ],
)
def test_active_layout_fields_are_required(
    builder: Builder,
    kwargs: dict[str, Any],
    field: str,
) -> None:
    incomplete = kwargs.copy()
    incomplete.pop(field)
    with pytest.raises(ValueError, match=field):
        builder(**incomplete)


@pytest.mark.parametrize(
    ("builder", "kwargs", "extra_field"),
    [
        (manualfx.build_subtype0, _SUBTYPE0_CCT, {"hue_seq_raw": 0}),
        (manualfx.build_subtype0, _SUBTYPE0_HSI, {"gm_seq_raw": 0}),
        (manualfx.build_subtype1, _SUBTYPE1_MODE0, {"fade_in_curve_raw": 0}),
        (manualfx.build_subtype1, _SUBTYPE1_MODE1, {"free_time_seq_raw": 0}),
        (manualfx.build_subtype1, _SUBTYPE1_MODE2, {"free_time_seq_raw": 0}),
        (manualfx.build_subtype2, _SUBTYPE2_MODE0, {"olp_seq_raw": 0}),
        (manualfx.build_subtype2, _SUBTYPE2_MODE1, {"frequency_seq_raw": 0}),
        (manualfx.build_subtype2, _SUBTYPE2_MODE2, {"unit_time_seq_raw": 0}),
    ],
)
def test_inactive_layout_fields_are_rejected(
    builder: Builder,
    kwargs: dict[str, Any],
    extra_field: dict[str, int],
) -> None:
    field = next(iter(extra_field))
    with pytest.raises(ValueError, match=field):
        builder(**(kwargs | extra_field))


@pytest.mark.parametrize("invalid", [True, 1.5, "1"])
def test_raw_fields_require_integers(invalid: object) -> None:
    with pytest.raises(TypeError, match="gm_seq_raw"):
        manualfx.build_subtype0(**(_SUBTYPE0_CCT | {"gm_seq_raw": invalid}))


@pytest.mark.parametrize("invalid", [-1, 2])
def test_option_rejects_values_outside_one_bit(invalid: int) -> None:
    with pytest.raises(ValueError, match="option"):
        manualfx.build_subtype3(**(_SUBTYPE3 | {"option": invalid}))


def _set_bit_and_fix_checksum(payload: bytes, bit: int) -> bytes:
    modified = bytearray(payload)
    modified[bit // 8] |= 1 << (bit % 8)
    modified[0] = sum(modified[1:]) & 0xFF
    return bytes(modified)


@pytest.mark.parametrize(
    ("payload", "reserved_bit"),
    [
        (manualfx.build_subtype0(**_SUBTYPE0_CCT), 8),
        (manualfx.build_subtype1(**_SUBTYPE1_MODE0), 8),
        (manualfx.build_subtype1(**_SUBTYPE1_MODE1), 8),
        (manualfx.build_subtype1(**_SUBTYPE1_MODE2), 8),
        (manualfx.build_subtype2(**_SUBTYPE2_MODE0), 8),
        (manualfx.build_subtype2(**_SUBTYPE2_MODE1), 8),
        (manualfx.build_subtype2(**_SUBTYPE2_MODE2), 8),
        (manualfx.build_subtype3(**_SUBTYPE3), 8),
    ],
)
def test_decoder_rejects_noncanonical_reserved_bits(
    payload: bytes,
    reserved_bit: int,
) -> None:
    assert manualfx.decode(_set_bit_and_fix_checksum(payload, reserved_bit)) is None


def test_subtype3_decoder_requires_fixed_effect_mode_two() -> None:
    payload = manualfx.build_subtype3(**_SUBTYPE3)
    # Setting bit 66 changes the Java packer's fixed two-bit value from 2 to 3.
    assert manualfx.decode(_set_bit_and_fix_checksum(payload, 66)) is None


def test_decoder_rejects_bad_type_length_checksum_and_command() -> None:
    payload = manualfx.build_subtype0(**_SUBTYPE0_CCT)
    assert manualfx.decode(bytearray(payload)) is None  # type: ignore[arg-type]
    assert manualfx.decode(payload[:-1]) is None

    bad_checksum = bytes([payload[0] ^ 1, *payload[1:]])
    assert manualfx.decode(bad_checksum) is None

    bad_command = bytearray(payload)
    bad_command[9] = (bad_command[9] & 0x80) | 30
    bad_command[0] = sum(bad_command[1:]) & 0xFF
    assert manualfx.decode(bytes(bad_command)) is None


def test_encode_rejects_unknown_objects_and_revalidates_dataclasses() -> None:
    with pytest.raises(TypeError, match="unsupported Manual FX message"):
        manualfx.encode(object())

    invalid = manualfx.ManualFxSubtype3(**(_SUBTYPE3 | {"ctrl_raw": 4}))
    with pytest.raises(ValueError, match="ctrl_raw"):
        manualfx.encode(invalid)
