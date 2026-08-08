"""Golden and validation tests for opaque commands 43 and 44."""

from __future__ import annotations

import pytest
from amaranble import motion


@pytest.mark.parametrize(
    ("operation", "expected_hex"),
    [
        (motion.MotionOperation.READ, "eb0123456789abcdef2b"),
        (motion.MotionOperation.WRITE, "6b0123456789abcdefab"),
    ],
)
def test_motion_config_golden_vectors(
    operation: motion.MotionOperation,
    expected_hex: str,
) -> None:
    data_raw = bytes.fromhex("0123456789abcdef")

    payload = motion.build_motion_config(data_raw, operation=operation)

    assert payload.hex() == expected_hex
    assert payload[0] == sum(payload[1:]) & 0xFF
    assert motion.decode_motion_config(bytes.fromhex(expected_hex)) == (
        motion.MotionConfigMessage(data_raw=data_raw, operation=operation)
    )
    assert motion.decode_motion(payload) == motion.MotionConfigMessage(
        data_raw=data_raw,
        operation=operation,
    )


@pytest.mark.parametrize(
    ("operation", "expected_hex"),
    [
        (motion.MotionOperation.READ, "6c10203040506070802c"),
        (motion.MotionOperation.WRITE, "ec1020304050607080ac"),
    ],
)
def test_motion_live_golden_vectors(
    operation: motion.MotionOperation,
    expected_hex: str,
) -> None:
    data_raw = bytes.fromhex("1020304050607080")

    payload = motion.build_motion_live(data_raw, operation=operation)

    assert payload.hex() == expected_hex
    assert payload[0] == sum(payload[1:]) & 0xFF
    assert motion.decode_motion_live(bytes.fromhex(expected_hex)) == (
        motion.MotionLiveMessage(data_raw=data_raw, operation=operation)
    )
    assert motion.decode_motion(payload) == motion.MotionLiveMessage(
        data_raw=data_raw,
        operation=operation,
    )


@pytest.mark.parametrize(
    "builder",
    [motion.build_motion_config, motion.build_motion_live],
)
def test_all_raw_data_bits_round_trip_exactly(builder: object) -> None:
    data_raw = bytes.fromhex("00ff55aa01807ffe")

    payload = builder(data_raw, operation=motion.MotionOperation.WRITE)
    decoded = motion.decode_motion(payload)

    assert decoded is not None
    assert decoded.data_raw == data_raw
    assert decoded.operation is motion.MotionOperation.WRITE


@pytest.mark.parametrize(
    "builder",
    [motion.build_motion_config, motion.build_motion_live],
)
@pytest.mark.parametrize("data_raw", [b"", bytes(7), bytes(9)])
def test_builder_rejects_wrong_raw_data_length(
    builder: object,
    data_raw: bytes,
) -> None:
    with pytest.raises(ValueError, match="exactly 8 bytes"):
        builder(data_raw, operation=motion.MotionOperation.READ)


@pytest.mark.parametrize(
    "builder",
    [motion.build_motion_config, motion.build_motion_live],
)
def test_builder_rejects_non_bytes_data(builder: object) -> None:
    with pytest.raises(TypeError, match="must be bytes"):
        builder(bytearray(8), operation=motion.MotionOperation.READ)


@pytest.mark.parametrize(
    "builder",
    [motion.build_motion_config, motion.build_motion_live],
)
@pytest.mark.parametrize("operation", [-1, 2, 255])
def test_builder_rejects_operation_outside_one_bit_range(
    builder: object,
    operation: int,
) -> None:
    with pytest.raises(ValueError):
        builder(bytes(8), operation=operation)


def test_operation_is_required_because_the_apk_has_no_proven_default() -> None:
    with pytest.raises(TypeError):
        motion.build_motion_config(bytes(8))
    with pytest.raises(TypeError):
        motion.build_motion_live(bytes(8))


@pytest.mark.parametrize(
    ("decoder", "valid"),
    [
        (
            motion.decode_motion_config,
            bytes.fromhex("eb0123456789abcdef2b"),
        ),
        (
            motion.decode_motion_live,
            bytes.fromhex("6c10203040506070802c"),
        ),
    ],
)
def test_decoder_rejects_wrong_length_checksum_and_command(
    decoder: object,
    valid: bytes,
) -> None:
    assert decoder(valid[:-1]) is None

    bad_checksum = bytearray(valid)
    bad_checksum[0] ^= 0x01
    assert decoder(bytes(bad_checksum)) is None

    wrong_command = bytearray(valid)
    wrong_command[9] = (wrong_command[9] & 0x80) | 45
    wrong_command[0] = sum(wrong_command[1:]) & 0xFF
    assert decoder(bytes(wrong_command)) is None


def test_generic_decoder_rejects_unknown_command_and_invalid_checksum() -> None:
    unknown = bytearray.fromhex("0001020304050607082d")
    unknown[0] = sum(unknown[1:]) & 0xFF
    assert motion.decode_motion(bytes(unknown)) is None

    bad_checksum = bytearray.fromhex("eb0123456789abcdef2b")
    bad_checksum[0] ^= 0x01
    assert motion.decode_motion(bytes(bad_checksum)) is None
