"""Independent golden tests for commands 3, 4, and 5."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from amaranble import steady_modes


def _assert_checksum(payload: bytes) -> None:
    assert len(payload) == 10
    assert payload[0] == sum(payload[1:]) & 0xFF


def test_constants_and_declared_enums_are_stable() -> None:
    assert steady_modes.CMD_GEL == 3
    assert steady_modes.CMD_RGBW == 4
    assert steady_modes.CMD_XY == 5
    assert list(steady_modes.SteadyOperation) == [
        steady_modes.SteadyOperation.READ,
        steady_modes.SteadyOperation.WRITE,
    ]
    assert list(steady_modes.GelOrigin) == [
        steady_modes.GelOrigin.LEE,
        steady_modes.GelOrigin.ROSCO,
    ]


def test_gel_java_layout_golden_vector_and_round_trip() -> None:
    expected = bytes.fromhex("ea01000060d50c63c283")
    state = steady_modes.GelState(
        on=True,
        operation=steady_modes.SteadyOperation.WRITE,
        intensity=777,
        cct_raw=560,
        origin=steady_modes.GelOrigin.ROSCO,
        gel_type=9,
        color=683,
    )

    payload = steady_modes.build_gel(
        intensity=state.intensity,
        cct_raw=state.cct_raw,
        origin=state.origin,
        gel_type=state.gel_type,
        color=state.color,
        on=state.on,
        operation=state.operation,
    )

    assert payload == expected
    _assert_checksum(payload)
    assert steady_modes.decode_gel(expected) == state
    assert steady_modes.decode_steady_mode(expected) == state


def test_rgbw_smali_layout_golden_vector_and_round_trip() -> None:
    expected = bytes.fromhex("a3b1761ffad0e72efa84")
    state = steady_modes.RGBWState(
        on=True,
        operation=steady_modes.SteadyOperation.WRITE,
        intensity=875,
        red_raw=1000,
        green_raw=750,
        blue_raw=500,
        warm_white_raw=250,
        cool_white_raw=125,
    )

    payload = steady_modes.build_rgbw(
        intensity=state.intensity,
        red_raw=state.red_raw,
        green_raw=state.green_raw,
        blue_raw=state.blue_raw,
        warm_white_raw=state.warm_white_raw,
        cool_white_raw=state.cool_white_raw,
        on=state.on,
        operation=state.operation,
    )

    assert payload == expected
    _assert_checksum(payload)
    assert steady_modes.decode_rgbw(expected) == state
    assert steady_modes.decode_steady_mode(expected) == state


def test_xy_java_layout_golden_vector_and_round_trip() -> None:
    expected = bytes.fromhex("2d010000204e1067c285")
    state = steady_modes.XYState(
        on=True,
        operation=steady_modes.SteadyOperation.WRITE,
        intensity=777,
        x_raw=10000,
        y_raw=5000,
    )

    payload = steady_modes.build_xy(
        intensity=state.intensity,
        x_raw=state.x_raw,
        y_raw=state.y_raw,
        on=state.on,
        operation=state.operation,
    )

    assert payload == expected
    _assert_checksum(payload)
    assert steady_modes.decode_xy(expected) == state
    assert steady_modes.decode_steady_mode(expected) == state


@pytest.mark.parametrize(
    ("payload_hex", "expected"),
    [
        (
            "6a01000060d50c63c203",
            steady_modes.GelState(
                True,
                steady_modes.SteadyOperation.READ,
                777,
                560,
                steady_modes.GelOrigin.ROSCO,
                9,
                683,
            ),
        ),
        (
            "23b1761ffad0e72efa04",
            steady_modes.RGBWState(
                True,
                steady_modes.SteadyOperation.READ,
                875,
                1000,
                750,
                500,
                250,
                125,
            ),
        ),
        (
            "ad010000204e1067c205",
            steady_modes.XYState(
                True,
                steady_modes.SteadyOperation.READ,
                777,
                10000,
                5000,
            ),
        ),
    ],
)
def test_read_report_golden_vectors(
    payload_hex: str,
    expected: steady_modes.SteadyModeState,
) -> None:
    payload = bytes.fromhex(payload_hex)
    _assert_checksum(payload)
    assert steady_modes.decode_steady_mode(payload) == expected


def test_gel_wire_boundaries_are_exact() -> None:
    minimum = steady_modes.build_gel(
        intensity=0,
        cct_raw=0,
        origin=steady_modes.GelOrigin.LEE,
        gel_type=0,
        color=0,
        on=False,
    )
    maximum = steady_modes.build_gel(
        intensity=1023,
        cct_raw=1023,
        origin=steady_modes.GelOrigin.ROSCO,
        gel_type=15,
        color=1023,
    )

    assert minimum.hex() == "83000000000000000083"
    assert maximum.hex() == "60010000e0ffffffff83"
    assert steady_modes.decode_gel(minimum) == steady_modes.GelState(
        False,
        steady_modes.SteadyOperation.WRITE,
        0,
        0,
        steady_modes.GelOrigin.LEE,
        0,
        0,
    )


def test_rgbw_wire_boundaries_are_exact() -> None:
    minimum = steady_modes.build_rgbw(
        intensity=0,
        red_raw=0,
        green_raw=0,
        blue_raw=0,
        on=False,
    )
    maximum = steady_modes.build_rgbw(
        intensity=1023,
        red_raw=1023,
        green_raw=1023,
        blue_raw=1023,
        warm_white_raw=1023,
        cool_white_raw=1023,
    )

    assert minimum.hex() == "84000000000000000084"
    assert maximum.hex() == "6ef1ffffffffffffff84"
    assert steady_modes.decode_rgbw(maximum) == steady_modes.RGBWState(
        True,
        steady_modes.SteadyOperation.WRITE,
        1023,
        1023,
        1023,
        1023,
        1023,
        1023,
    )


def test_xy_wire_boundaries_are_exact() -> None:
    minimum = steady_modes.build_xy(
        intensity=0,
        x_raw=0,
        y_raw=0,
        on=False,
    )
    maximum = steady_modes.build_xy(
        intensity=1023,
        x_raw=16383,
        y_raw=16383,
    )

    assert minimum.hex() == "85000000000000000085"
    assert maximum.hex() == "7e010000fcffffffff85"
    assert steady_modes.decode_xy(maximum) == steady_modes.XYState(
        True,
        steady_modes.SteadyOperation.WRITE,
        1023,
        16383,
        16383,
    )


@pytest.mark.parametrize(
    ("builder", "kwargs"),
    [
        (
            steady_modes.build_gel,
            {"intensity": -1, "cct_raw": 0, "origin": 0, "gel_type": 0, "color": 0},
        ),
        (
            steady_modes.build_gel,
            {"intensity": 0, "cct_raw": 1024, "origin": 0, "gel_type": 0, "color": 0},
        ),
        (
            steady_modes.build_gel,
            {"intensity": 0, "cct_raw": 0, "origin": 0, "gel_type": 16, "color": 0},
        ),
        (
            steady_modes.build_gel,
            {"intensity": 0, "cct_raw": 0, "origin": 0, "gel_type": 0, "color": 1024},
        ),
        (
            steady_modes.build_rgbw,
            {"intensity": 1024, "red_raw": 0, "green_raw": 0, "blue_raw": 0},
        ),
        (
            steady_modes.build_rgbw,
            {"intensity": 0, "red_raw": -1, "green_raw": 0, "blue_raw": 0},
        ),
        (
            steady_modes.build_rgbw,
            {"intensity": 0, "red_raw": 0, "green_raw": 1024, "blue_raw": 0},
        ),
        (
            steady_modes.build_rgbw,
            {"intensity": 0, "red_raw": 0, "green_raw": 0, "blue_raw": 1024},
        ),
        (
            steady_modes.build_xy,
            {"intensity": -1, "x_raw": 0, "y_raw": 0},
        ),
        (
            steady_modes.build_xy,
            {"intensity": 0, "x_raw": 16384, "y_raw": 0},
        ),
        (
            steady_modes.build_xy,
            {"intensity": 0, "x_raw": 0, "y_raw": 16384},
        ),
    ],
)
def test_out_of_range_raw_fields_are_rejected(
    builder: Callable[..., bytes],
    kwargs: dict[str, int],
) -> None:
    with pytest.raises(ValueError):
        builder(**kwargs)


def test_invalid_discriminators_and_non_integer_fields_are_rejected() -> None:
    with pytest.raises(ValueError):
        steady_modes.build_gel(
            intensity=0,
            cct_raw=0,
            origin=2,
            gel_type=0,
            color=0,
        )
    with pytest.raises(ValueError):
        steady_modes.build_xy(intensity=0, x_raw=0, y_raw=0, operation=2)
    with pytest.raises(ValueError):
        steady_modes.build_xy(intensity=0, x_raw=0, y_raw=0, on=2)
    with pytest.raises(TypeError):
        steady_modes.build_xy(intensity=0, x_raw=1.5, y_raw=0)  # type: ignore[arg-type]


def test_decoders_reject_length_checksum_and_other_commands() -> None:
    valid = steady_modes.build_rgbw(
        intensity=500,
        red_raw=100,
        green_raw=200,
        blue_raw=300,
    )
    assert steady_modes.decode_rgbw(valid[:-1]) is None
    assert steady_modes.decode_steady_mode(valid[:-1]) is None

    bad_checksum = bytearray(valid)
    bad_checksum[0] ^= 1
    assert steady_modes.decode_rgbw(bytes(bad_checksum)) is None
    assert steady_modes.decode_steady_mode(bytes(bad_checksum)) is None

    assert steady_modes.decode_gel(valid) is None
    assert steady_modes.decode_xy(valid) is None

    other = bytearray(valid)
    other[9] = 6 | 0x80
    other[0] = sum(other[1:]) & 0xFF
    assert steady_modes.decode_steady_mode(bytes(other)) is None


def test_reserved_bits_are_ignored_like_the_app_parsers() -> None:
    expected = steady_modes.XYState(
        True,
        steady_modes.SteadyOperation.READ,
        777,
        10000,
        5000,
    )
    payload = bytearray.fromhex("ad010000204e1067c205")
    payload[2] |= 0x80
    payload[0] = sum(payload[1:]) & 0xFF

    assert steady_modes.decode_xy(bytes(payload)) == expected
