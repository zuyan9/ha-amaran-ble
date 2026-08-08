"""Golden vectors for Telink multi/Magic Pixel commands 39 through 42."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from amaranble import multipixel


def _assert_round_trip(
    payload: bytes,
    expected_hex: str,
    expected_message: object,
) -> None:
    assert payload.hex() == expected_hex
    assert payload[0] == sum(payload[1:]) & 0xFF
    assert multipixel.decode(payload) == expected_message
    assert multipixel.encode(expected_message) == payload


def test_command_and_effect_ids_are_exact() -> None:
    assert multipixel.CMD_MULTI_PIXEL_PRE_CONFIG == 39
    assert multipixel.CMD_MULTI_PIXEL_CONFIG == 40
    assert multipixel.CMD_MULTI_PIXEL_CONFIG_RETRY == 41
    assert multipixel.CMD_MULTI_PIXEL_EFFECT == 42
    assert list(multipixel.MultiPixelEffectType) == [
        multipixel.MultiPixelEffectType.RAINBOW,
        multipixel.MultiPixelEffectType.INTEGRAL,
        multipixel.MultiPixelEffectType.PIXEL_MOVE,
        multipixel.MultiPixelEffectType.ADVANCED_MOVE,
    ]
    assert [int(value) for value in multipixel.MultiPixelEffectType] == [0, 1, 2, 3]


# These byte strings are fixed independently of the production encoder from
# the Java/smali field widths and LSB-first offsets. There are no APK defaults.
@pytest.mark.parametrize(
    ("payload", "expected_hex", "expected_message"),
    [
        (
            multipixel.build_pre_config(2),
            "270000000000000080a7",
            multipixel.MultiPixelPreConfig(2),
        ),
        (
            multipixel.build_pre_config(
                3,
                option=multipixel.MultiPixelOption.ACK_OR_READ,
            ),
            "e700000000000000c027",
            multipixel.MultiPixelPreConfig(
                3,
                option=multipixel.MultiPixelOption.ACK_OR_READ,
            ),
        ),
        (
            multipixel.build_config(
                group_id=17,
                shape_style=9,
                pixel_total=96,
                node1_id=0x123,
                pixel_start1=5,
                node2_id=0xABC,
                pixel_offset2=2,
                node3_id=0x789,
                pixel_offset3=5,
            ),
            "0d4d3c79b53012e08ca8",
            multipixel.MultiPixelConfig(
                group_id=17,
                shape_style=9,
                pixel_total=96,
                node1_id=0x123,
                pixel_start1=5,
                node2_id=0xABC,
                pixel_offset2=2,
                node3_id=0x789,
                pixel_offset3=5,
            ),
        ),
        (
            multipixel.build_config_retry(
                group_id=18,
                shape_style=10,
                pixel_total=97,
                node1_id=0x234,
                pixel_start1=6,
                node2_id=0xBCD,
                pixel_start2=7,
            ),
            "8f001c9ad740236195a9",
            multipixel.MultiPixelConfigRetry(
                group_id=18,
                shape_style=10,
                pixel_total=97,
                node1_id=0x234,
                pixel_start1=6,
                node2_id=0xBCD,
                pixel_start2=7,
            ),
        ),
        (
            multipixel.build_config_result(
                multipixel.MultiPixelCommand.CONFIG,
                10,
            ),
            "c800000000000000a028",
            multipixel.MultiPixelConfigResult(
                multipixel.MultiPixelCommand.CONFIG,
                10,
            ),
        ),
        (
            multipixel.build_config_result(
                multipixel.MultiPixelCommand.CONFIG_RETRY,
                5,
                opaque_raw=0x123456789ABCDEF,
            ),
            "39efcdab896745235129",
            multipixel.MultiPixelConfigResult(
                multipixel.MultiPixelCommand.CONFIG_RETRY,
                5,
                opaque_raw=0x123456789ABCDEF,
            ),
        ),
    ],
)
def test_configuration_golden_vectors(
    payload: bytes,
    expected_hex: str,
    expected_message: object,
) -> None:
    _assert_round_trip(payload, expected_hex, expected_message)


@pytest.mark.parametrize(
    ("effect_type", "group_id", "state", "payload_raw", "expected_hex"),
    [
        (
            multipixel.MultiPixelEffectType.RAINBOW,
            19,
            0,
            0x123456789ABCD,
            "13cdab896745230198aa",
        ),
        (
            multipixel.MultiPixelEffectType.INTEGRAL,
            20,
            1,
            0x123456789ABCE,
            "bcceab89674523a1a0aa",
        ),
        (
            multipixel.MultiPixelEffectType.PIXEL_MOVE,
            21,
            2,
            0x123456789ABCF,
            "66cfab8967452341a9aa",
        ),
        (
            multipixel.MultiPixelEffectType.ADVANCED_MOVE,
            22,
            3,
            0x123456789ABD0,
            "0fd0ab89674523e1b1aa",
        ),
    ],
)
def test_effect_type_golden_vectors_and_round_trip(
    effect_type: multipixel.MultiPixelEffectType,
    group_id: int,
    state: int,
    payload_raw: int,
    expected_hex: str,
) -> None:
    payload = multipixel.build_effect(
        effect_type,
        group_id=group_id,
        state=state,
        payload_raw=payload_raw,
    )
    _assert_round_trip(
        payload,
        expected_hex,
        multipixel.MultiPixelEffect(
            effect_type=effect_type,
            group_id=group_id,
            state=state,
            payload_raw=payload_raw,
        ),
    )


def test_effect_ack_or_read_form_preserves_all_raw_bits() -> None:
    payload = multipixel.build_effect(
        multipixel.MultiPixelEffectType.ADVANCED_MOVE,
        group_id=31,
        state=3,
        payload_raw=(1 << 53) - 1,
        option=multipixel.MultiPixelOption.ACK_OR_READ,
    )
    _assert_round_trip(
        payload,
        "1cfffffffffffffff92a",
        multipixel.MultiPixelEffect(
            effect_type=multipixel.MultiPixelEffectType.ADVANCED_MOVE,
            group_id=31,
            state=3,
            payload_raw=(1 << 53) - 1,
            option=multipixel.MultiPixelOption.ACK_OR_READ,
        ),
    )


def test_dedicated_decoders_reject_other_family_members() -> None:
    pre_config = multipixel.build_pre_config(1)
    config = multipixel.build_config(**_CONFIG_BASE)
    retry = multipixel.build_config_retry(**_RETRY_BASE)
    effect = multipixel.build_effect(
        multipixel.MultiPixelEffectType.RAINBOW,
        group_id=0,
        state=0,
        payload_raw=0,
    )

    assert multipixel.decode_pre_config(pre_config) == multipixel.decode(pre_config)
    assert multipixel.decode_config(config) == multipixel.decode(config)
    assert multipixel.decode_config_retry(retry) == multipixel.decode(retry)
    assert multipixel.decode_effect(effect) == multipixel.decode(effect)
    assert multipixel.decode_pre_config(config) is None
    assert multipixel.decode_config(retry) is None
    assert multipixel.decode_config_retry(effect) is None
    assert multipixel.decode_effect(pre_config) is None


_CONFIG_BASE = {
    "group_id": 0,
    "shape_style": 0,
    "pixel_total": 0,
    "node1_id": 0,
    "pixel_start1": 0,
    "node2_id": 0,
    "pixel_offset2": 0,
    "node3_id": 0,
    "pixel_offset3": 0,
}


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("group_id", 32),
        ("shape_style", 16),
        ("pixel_total", 128),
        ("node1_id", 4096),
        ("pixel_start1", 128),
        ("node2_id", 4096),
        ("pixel_offset2", 4),
        ("node3_id", 4096),
        ("pixel_offset3", 8),
    ],
)
def test_config_builder_rejects_values_outside_wire_width(
    field: str,
    invalid: int,
) -> None:
    values = dict(_CONFIG_BASE)
    values[field] = invalid
    with pytest.raises(ValueError, match=field):
        multipixel.build_config(**values)


_RETRY_BASE = {
    "group_id": 0,
    "shape_style": 0,
    "pixel_total": 0,
    "node1_id": 0,
    "pixel_start1": 0,
    "node2_id": 0,
    "pixel_start2": 0,
}


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("group_id", -1),
        ("shape_style", 16),
        ("pixel_total", 128),
        ("node1_id", 4096),
        ("pixel_start1", 128),
        ("node2_id", 4096),
        ("pixel_start2", 128),
    ],
)
def test_retry_builder_rejects_values_outside_wire_width(
    field: str,
    invalid: int,
) -> None:
    values = dict(_RETRY_BASE)
    values[field] = invalid
    with pytest.raises(ValueError, match=field):
        multipixel.build_config_retry(**values)


@pytest.mark.parametrize(
    "builder",
    [
        lambda: multipixel.build_pre_config(4),
        lambda: multipixel.build_config_result(
            multipixel.MultiPixelCommand.CONFIG,
            16,
        ),
        lambda: multipixel.build_config_result(
            multipixel.MultiPixelCommand.CONFIG,
            0,
            opaque_raw=1 << 60,
        ),
        lambda: multipixel.build_effect(4, group_id=0, state=0, payload_raw=0),
        lambda: multipixel.build_effect(
            multipixel.MultiPixelEffectType.RAINBOW,
            group_id=32,
            state=0,
            payload_raw=0,
        ),
        lambda: multipixel.build_effect(
            multipixel.MultiPixelEffectType.RAINBOW,
            group_id=0,
            state=4,
            payload_raw=0,
        ),
        lambda: multipixel.build_effect(
            multipixel.MultiPixelEffectType.RAINBOW,
            group_id=0,
            state=0,
            payload_raw=1 << 53,
        ),
    ],
)
def test_other_builders_reject_out_of_width_values(
    builder: Callable[[], bytes],
) -> None:
    with pytest.raises(ValueError):
        builder()


def test_strict_type_and_result_command_validation() -> None:
    with pytest.raises(TypeError, match="config_type must be an integer"):
        multipixel.build_pre_config(1.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="command 40 or 41"):
        multipixel.build_config_result(multipixel.MultiPixelCommand.EFFECT, 0)
    with pytest.raises(TypeError, match="unsupported multi-pixel message"):
        multipixel.encode(object())


def _replace_bits(payload: bytes, value: int, start: int, width: int) -> bytes:
    packet = int.from_bytes(payload, "little")
    mask = ((1 << width) - 1) << start
    packet = (packet & ~mask) | ((value << start) & mask)
    result = bytearray(packet.to_bytes(10, "little"))
    result[0] = sum(result[1:]) & 0xFF
    return bytes(result)


def test_decode_rejects_malformed_and_noncanonical_packets() -> None:
    pre_config = multipixel.build_pre_config(1)
    invalid_checksum = bytearray(pre_config)
    invalid_checksum[1] ^= 1
    assert multipixel.decode(bytes(invalid_checksum)) is None
    assert multipixel.decode(bytearray(pre_config)) is None  # type: ignore[arg-type]
    assert multipixel.decode(b"short") is None

    unknown_command = bytearray(pre_config)
    unknown_command[9] = 0x80 | 38
    unknown_command[0] = sum(unknown_command[1:]) & 0xFF
    assert multipixel.decode(bytes(unknown_command)) is None

    pre_config_reserved = _replace_bits(pre_config, 1, 8, 1)
    assert multipixel.decode(pre_config_reserved) is None

    retry_reserved = _replace_bits(
        multipixel.build_config_retry(**_RETRY_BASE),
        1,
        8,
        1,
    )
    assert multipixel.decode(retry_reserved) is None

    unknown_effect = _replace_bits(
        multipixel.build_effect(
            multipixel.MultiPixelEffectType.RAINBOW,
            group_id=0,
            state=0,
            payload_raw=0,
        ),
        4,
        63,
        4,
    )
    assert multipixel.decode(unknown_effect) is None
