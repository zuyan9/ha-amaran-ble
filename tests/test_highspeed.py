"""Golden and boundary tests for command-53 high-speed photography."""

from __future__ import annotations

import pytest
from amaranble import highspeed


@pytest.mark.parametrize(
    ("state", "expected_hex"),
    [
        (highspeed.HighSpeedState.OFF, "35000000000000000035"),
        (highspeed.HighSpeedState.ON, "b5000000000000008035"),
    ],
)
def test_app_default_golden_vectors(
    state: highspeed.HighSpeedState,
    expected_hex: str,
) -> None:
    """Match HighSpeedProtocol.getSendData(), including its opaque top bit."""
    payload = highspeed.build_high_speed(state)

    assert payload.hex() == expected_hex
    assert payload[0] == sum(payload[1:]) & 0xFF
    assert payload[9] == highspeed.CMD_HIGH_SPEED
    assert highspeed.decode_high_speed(payload) == highspeed.HighSpeedMessage(
        state=state,
        operation=highspeed.HighSpeedOperation.APP_DEFAULT,
    )


@pytest.mark.parametrize(
    ("state", "expected_hex"),
    [
        (highspeed.HighSpeedState.OFF, "b50000000000000000b5"),
        (highspeed.HighSpeedState.ON, "350000000000000080b5"),
    ],
)
def test_opaque_one_golden_vectors_and_round_trip(
    state: highspeed.HighSpeedState,
    expected_hex: str,
) -> None:
    payload = highspeed.build_high_speed(
        state,
        operation=highspeed.HighSpeedOperation.OPAQUE_1,
    )

    assert payload.hex() == expected_hex
    decoded = highspeed.decode_high_speed(bytes.fromhex(expected_hex))
    assert decoded == highspeed.HighSpeedMessage(
        state=state,
        operation=highspeed.HighSpeedOperation.OPAQUE_1,
    )
    assert decoded is not None
    assert decoded.enabled is (state is highspeed.HighSpeedState.ON)
    assert (
        highspeed.build_high_speed(
            decoded.state,
            operation=decoded.operation,
        )
        == payload
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (False, highspeed.HighSpeedState.OFF),
        (True, highspeed.HighSpeedState.ON),
        (0, highspeed.HighSpeedState.OFF),
        (1, highspeed.HighSpeedState.ON),
    ],
)
def test_state_boolean_and_integer_boundaries(
    value: bool | int,
    expected: highspeed.HighSpeedState,
) -> None:
    decoded = highspeed.decode_high_speed(highspeed.build_high_speed(value))
    assert decoded is not None
    assert decoded.state is expected


@pytest.mark.parametrize("state", [-1, 2, 255])
def test_state_outside_one_bit_range_is_rejected(state: int) -> None:
    with pytest.raises(ValueError):
        highspeed.build_high_speed(state)


@pytest.mark.parametrize("operation", [-1, 2, 255])
def test_operation_outside_one_bit_range_is_rejected(operation: int) -> None:
    with pytest.raises(ValueError):
        highspeed.build_high_speed(highspeed.HighSpeedState.ON, operation=operation)


def test_decoder_rejects_wrong_length_checksum_and_command() -> None:
    valid = highspeed.build_high_speed(highspeed.HighSpeedState.ON)

    assert highspeed.decode_high_speed(valid[:-1]) is None

    bad_checksum = bytearray(valid)
    bad_checksum[0] ^= 0x01
    assert highspeed.decode_high_speed(bytes(bad_checksum)) is None

    wrong_command = bytearray(valid)
    wrong_command[9] = (wrong_command[9] & 0x80) | 54
    wrong_command[0] = sum(wrong_command[1:]) & 0xFF
    assert highspeed.decode_high_speed(bytes(wrong_command)) is None


def test_reserved_bits_do_not_change_state_or_opaque_operation() -> None:
    """The Java state parser reads only bit 71; preserve that tolerance."""
    payload = bytearray.fromhex("000100000000000080b5")
    payload[0] = sum(payload[1:]) & 0xFF

    assert highspeed.decode_high_speed(bytes(payload)) == highspeed.HighSpeedMessage(
        state=highspeed.HighSpeedState.ON,
        operation=highspeed.HighSpeedOperation.OPAQUE_1,
    )
