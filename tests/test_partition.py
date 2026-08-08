"""Independent golden tests for legacy partition commands 35, 36, and 38."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from amaranble import partition


def _assert_checksum(payload: bytes) -> None:
    assert len(payload) == 10
    assert payload[0] == sum(payload[1:]) & 0xFF


def _with_checksum(payload: bytearray) -> bytes:
    payload[0] = sum(payload[1:]) & 0xFF
    return bytes(payload)


def test_constants_and_proven_enums_are_stable() -> None:
    assert partition.CMD_PARTITION_COLOR == 35
    assert partition.CMD_PARTITION_EFFECT == 36
    assert partition.CMD_PARTITION_CONFIG == 38
    assert list(partition.PartitionOperation) == [
        partition.PartitionOperation.READ,
        partition.PartitionOperation.WRITE,
    ]
    assert list(partition.PartitionLightMode) == [
        partition.PartitionLightMode.CCT,
        partition.PartitionLightMode.HSI,
    ]


def test_color_cct_sender_layout_and_parser_asymmetry_golden_vector() -> None:
    # Independently derived from getSendData(): DUV byte first, then CCT byte.
    expected = bytes.fromhex("02c870091b80818181a3")
    indexes = (0, 7, 8, 15, 16, 23, 24, 35)

    payload = partition.build_partition_color_cct(
        indexes=indexes,
        intensity_raw=777,
        cct_raw=112,
        duv_raw=200,
        fx_state_raw=1,
    )

    assert payload == expected
    _assert_checksum(payload)
    # The APK parser names bits 8..15 CCT and bits 16..23 DUV, opposite send.
    state = partition.PartitionColorState(
        light_mode=partition.PartitionLightMode.CCT,
        intensity_raw=777,
        fx_state_raw=1,
        indexes=indexes,
        cct_raw=200,
        duv_raw=112,
    )
    assert partition.decode_partition_color(expected) == state
    assert partition.decode_partition(expected) == state

    # Supplying the inverse parser values reproduces the same outgoing bytes.
    assert (
        partition.build_partition_color_cct(
            indexes=state.indexes,
            intensity_raw=state.intensity_raw,
            cct_raw=state.duv_raw,
            duv_raw=state.cct_raw,
            fx_state_raw=state.fx_state_raw,
        )
        == expected
    )


def test_color_hsi_golden_vector_and_round_trip() -> None:
    expected = bytes.fromhex("526396002640000848a3")
    state = partition.PartitionColorState(
        light_mode=partition.PartitionLightMode.HSI,
        intensity_raw=512,
        fx_state_raw=0,
        indexes=(1, 4, 12, 25, 34),
        hue_raw=300,
        saturation_raw=99,
    )

    payload = partition.build_partition_color_hsi(
        indexes=state.indexes,
        intensity_raw=state.intensity_raw,
        hue_raw=300,
        saturation_raw=99,
        fx_state_raw=state.fx_state_raw,
    )

    assert payload == expected
    _assert_checksum(payload)
    assert partition.decode_partition_color(expected) == state
    assert partition.decode_partition(expected) == state


def test_partition_index_wire_order_covers_both_ends() -> None:
    first = partition.build_partition_color_cct(
        indexes=[0],
        intensity_raw=0,
        cct_raw=0,
        duv_raw=0,
        fx_state_raw=0,
    )
    last = partition.build_partition_color_cct(
        indexes=[35],
        intensity_raw=0,
        cct_raw=0,
        duv_raw=0,
        fx_state_raw=0,
    )

    assert first.hex() == "230000000000000080a3"
    assert last.hex() == "b30000001000000000a3"
    assert partition.decode_partition_color(first).indexes == (0,)  # type: ignore[union-attr]
    assert partition.decode_partition_color(last).indexes == (35,)  # type: ignore[union-attr]


def test_effect_sender_layout_and_trigger_asymmetry_golden_vector() -> None:
    # trigger_mode_input_raw=0 is encoded as the wire bit one.
    expected = bytes.fromhex("7f0000d531915b6386a4")
    payload = partition.build_partition_effect(
        intensity_min_raw=85,
        trigger_mode_input_raw=0,
        frequency_max_raw=17,
        frequency_min_raw=9,
        interval_max_raw=100,
        interval_min_raw=45,
        lasting_max_raw=99,
        lasting_min_raw=12,
        fx_mode_raw=2,
        operation=partition.PartitionOperation.WRITE,
    )

    assert payload == expected
    _assert_checksum(payload)
    state = partition.PartitionEffectState(
        operation=partition.PartitionOperation.WRITE,
        intensity_min_raw=85,
        trigger_mode_wire_raw=1,
        frequency_max_raw=17,
        frequency_min_raw=9,
        interval_max_raw=100,
        interval_min_raw=45,
        lasting_max_raw=99,
        lasting_min_raw=12,
        fx_mode_raw=2,
    )
    assert partition.decode_partition_effect(expected) == state
    assert partition.decode_partition(expected) == state
    assert state.trigger_mode_input_raw == 0
    assert (
        partition.build_partition_effect(
            intensity_min_raw=state.intensity_min_raw,
            trigger_mode_input_raw=state.trigger_mode_input_raw,
            frequency_max_raw=state.frequency_max_raw,
            frequency_min_raw=state.frequency_min_raw,
            interval_max_raw=state.interval_max_raw,
            interval_min_raw=state.interval_min_raw,
            lasting_max_raw=state.lasting_max_raw,
            lasting_min_raw=state.lasting_min_raw,
            fx_mode_raw=state.fx_mode_raw,
            operation=state.operation,
        )
        == expected
    )


def test_effect_wire_boundaries_are_exact() -> None:
    minimum = partition.build_partition_effect(
        intensity_min_raw=0,
        trigger_mode_input_raw=0,
        frequency_max_raw=0,
        frequency_min_raw=0,
        interval_max_raw=0,
        interval_min_raw=0,
        lasting_max_raw=0,
        lasting_min_raw=0,
        fx_mode_raw=0,
        operation=partition.PartitionOperation.READ,
    )
    maximum = partition.build_partition_effect(
        intensity_min_raw=127,
        trigger_mode_input_raw=1,
        frequency_max_raw=31,
        frequency_min_raw=31,
        interval_max_raw=127,
        interval_min_raw=127,
        lasting_max_raw=127,
        lasting_min_raw=127,
        fx_mode_raw=3,
        operation=partition.PartitionOperation.WRITE,
    )

    assert minimum.hex() == "a4000080000000000024"
    assert maximum.hex() == "1e00007fffffffffffa4"
    assert partition.decode_partition_effect(minimum) == partition.PartitionEffectState(
        operation=partition.PartitionOperation.READ,
        intensity_min_raw=0,
        trigger_mode_wire_raw=1,
        frequency_max_raw=0,
        frequency_min_raw=0,
        interval_max_raw=0,
        interval_min_raw=0,
        lasting_max_raw=0,
        lasting_min_raw=0,
        fx_mode_raw=0,
    )
    assert partition.decode_partition_effect(maximum) == partition.PartitionEffectState(
        operation=partition.PartitionOperation.WRITE,
        intensity_min_raw=127,
        trigger_mode_wire_raw=0,
        frequency_max_raw=31,
        frequency_min_raw=31,
        interval_max_raw=127,
        interval_min_raw=127,
        lasting_max_raw=127,
        lasting_min_raw=127,
        fx_mode_raw=3,
    )


def test_config_read_and_write_golden_vectors() -> None:
    read = partition.build_partition_config_read()
    write = partition.build_partition_config_write(10)

    assert read.hex() == "26000000000000000026"
    assert write.hex() == "4600000000000000a0a6"
    _assert_checksum(read)
    _assert_checksum(write)
    assert partition.decode_partition_config(read) == partition.PartitionConfigState(
        operation=partition.PartitionOperation.READ,
        xy_mode_raw=0,
        pixel_x1_raw=0,
        pixel_y1_raw=0,
        pixel_x2_raw=0,
        pixel_y2_raw=0,
    )
    assert partition.decode_partition(write) == partition.PartitionConfigState(
        operation=partition.PartitionOperation.WRITE,
        xy_mode_raw=10,
        pixel_x1_raw=0,
        pixel_y1_raw=0,
        pixel_x2_raw=0,
        pixel_y2_raw=0,
    )


def test_config_parser_only_geometry_report_golden_vector() -> None:
    # Geometry is Y2[48:54], X2[54:60], Y1[60:64], X1[64:68].
    report = bytes.fromhex("af0000000000955a9a26")

    _assert_checksum(report)
    assert partition.decode_partition_config(report) == partition.PartitionConfigState(
        operation=partition.PartitionOperation.READ,
        xy_mode_raw=9,
        pixel_x1_raw=10,
        pixel_y1_raw=5,
        pixel_x2_raw=42,
        pixel_y2_raw=21,
    )


@pytest.mark.parametrize(
    ("builder", "kwargs"),
    [
        (
            partition.build_partition_color_cct,
            {
                "indexes": (),
                "intensity_raw": 1024,
                "cct_raw": 0,
                "duv_raw": 0,
                "fx_state_raw": 0,
            },
        ),
        (
            partition.build_partition_color_cct,
            {
                "indexes": (),
                "intensity_raw": 0,
                "cct_raw": 256,
                "duv_raw": 0,
                "fx_state_raw": 0,
            },
        ),
        (
            partition.build_partition_color_hsi,
            {
                "indexes": (),
                "intensity_raw": 0,
                "hue_raw": 512,
                "saturation_raw": 0,
                "fx_state_raw": 0,
            },
        ),
        (
            partition.build_partition_color_hsi,
            {
                "indexes": (),
                "intensity_raw": 0,
                "hue_raw": 0,
                "saturation_raw": 128,
                "fx_state_raw": 0,
            },
        ),
        (
            partition.build_partition_effect,
            {
                "intensity_min_raw": 128,
                "trigger_mode_input_raw": 0,
                "frequency_max_raw": 0,
                "frequency_min_raw": 0,
                "interval_max_raw": 0,
                "interval_min_raw": 0,
                "lasting_max_raw": 0,
                "lasting_min_raw": 0,
                "fx_mode_raw": 0,
                "operation": 0,
            },
        ),
        (
            partition.build_partition_effect,
            {
                "intensity_min_raw": 0,
                "trigger_mode_input_raw": 2,
                "frequency_max_raw": 0,
                "frequency_min_raw": 0,
                "interval_max_raw": 0,
                "interval_min_raw": 0,
                "lasting_max_raw": 0,
                "lasting_min_raw": 0,
                "fx_mode_raw": 0,
                "operation": 0,
            },
        ),
        (
            partition.build_partition_effect,
            {
                "intensity_min_raw": 0,
                "trigger_mode_input_raw": 0,
                "frequency_max_raw": 32,
                "frequency_min_raw": 0,
                "interval_max_raw": 0,
                "interval_min_raw": 0,
                "lasting_max_raw": 0,
                "lasting_min_raw": 0,
                "fx_mode_raw": 0,
                "operation": 0,
            },
        ),
        (
            partition.build_partition_effect,
            {
                "intensity_min_raw": 0,
                "trigger_mode_input_raw": 0,
                "frequency_max_raw": 0,
                "frequency_min_raw": 0,
                "interval_max_raw": 128,
                "interval_min_raw": 0,
                "lasting_max_raw": 0,
                "lasting_min_raw": 0,
                "fx_mode_raw": 0,
                "operation": 0,
            },
        ),
        (
            partition.build_partition_effect,
            {
                "intensity_min_raw": 0,
                "trigger_mode_input_raw": 0,
                "frequency_max_raw": 0,
                "frequency_min_raw": 0,
                "interval_max_raw": 0,
                "interval_min_raw": 0,
                "lasting_max_raw": 0,
                "lasting_min_raw": 0,
                "fx_mode_raw": 4,
                "operation": 0,
            },
        ),
        (partition.build_partition_config_write, {"xy_mode_raw": 16}),
    ],
)
def test_out_of_range_fields_are_rejected(
    builder: Callable[..., bytes],
    kwargs: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        builder(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("indexes", [[-1], [36], [4, 4]])
def test_invalid_partition_indexes_are_rejected(indexes: list[int]) -> None:
    with pytest.raises(ValueError):
        partition.build_partition_color_cct(
            indexes=indexes,
            intensity_raw=0,
            cct_raw=0,
            duv_raw=0,
            fx_state_raw=0,
        )


def test_non_integer_fields_and_indexes_are_rejected() -> None:
    with pytest.raises(TypeError):
        partition.build_partition_color_hsi(
            indexes=[1.5],  # type: ignore[list-item]
            intensity_raw=0,
            hue_raw=0,
            saturation_raw=0,
            fx_state_raw=0,
        )
    with pytest.raises(TypeError):
        partition.build_partition_color_cct(
            indexes=4,  # type: ignore[arg-type]
            intensity_raw=0,
            cct_raw=0,
            duv_raw=0,
            fx_state_raw=0,
        )
    with pytest.raises(TypeError):
        partition.build_partition_config_write(1.5)  # type: ignore[arg-type]


def test_invalid_operation_is_rejected() -> None:
    kwargs = {
        "intensity_min_raw": 0,
        "trigger_mode_input_raw": 0,
        "frequency_max_raw": 0,
        "frequency_min_raw": 0,
        "interval_max_raw": 0,
        "interval_min_raw": 0,
        "lasting_max_raw": 0,
        "lasting_min_raw": 0,
        "fx_mode_raw": 0,
    }
    with pytest.raises(ValueError):
        partition.build_partition_effect(**kwargs, operation=2)


def test_decoders_reject_length_checksum_wrong_command_and_non_bytes() -> None:
    valid = partition.build_partition_effect(
        intensity_min_raw=1,
        trigger_mode_input_raw=1,
        frequency_max_raw=2,
        frequency_min_raw=3,
        interval_max_raw=4,
        interval_min_raw=5,
        lasting_max_raw=6,
        lasting_min_raw=7,
        fx_mode_raw=1,
        operation=partition.PartitionOperation.WRITE,
    )

    assert partition.decode_partition_effect(valid[:-1]) is None
    assert partition.decode_partition(valid[:-1]) is None

    bad_checksum = bytearray(valid)
    bad_checksum[0] ^= 1
    assert partition.decode_partition_effect(bytes(bad_checksum)) is None
    assert partition.decode_partition(bytes(bad_checksum)) is None

    wrong_command = bytearray(valid)
    wrong_command[9] = (wrong_command[9] & 0x80) | 37
    wrong_command = bytearray(_with_checksum(wrong_command))
    assert partition.decode_partition(bytes(wrong_command)) is None

    assert partition.decode_partition_config(valid) is None
    assert partition.decode_partition_color(valid) is None
    assert partition.decode_partition(bytearray(valid)) is None  # type: ignore[arg-type]


def test_decoders_strictly_reject_fixed_and_reserved_bits() -> None:
    color = bytearray(
        partition.build_partition_color_cct(
            indexes=(),
            intensity_raw=0,
            cct_raw=0,
            duv_raw=0,
            fx_state_raw=0,
        )
    )
    color[9] &= 0x7F  # command 35's final bit is a fixed literal one.
    assert partition.decode_partition_color(_with_checksum(color)) is None

    effect = bytearray(
        partition.build_partition_effect(
            intensity_min_raw=0,
            trigger_mode_input_raw=0,
            frequency_max_raw=0,
            frequency_min_raw=0,
            interval_max_raw=0,
            interval_min_raw=0,
            lasting_max_raw=0,
            lasting_min_raw=0,
            fx_mode_raw=0,
            operation=partition.PartitionOperation.READ,
        )
    )
    effect[1] = 1  # bits 8..23 are fixed zero in command 36.
    assert partition.decode_partition_effect(_with_checksum(effect)) is None

    config = bytearray(partition.build_partition_config_write(3))
    config[5] = 1  # bits 8..47 are fixed zero in command 38.
    assert partition.decode_partition_config(_with_checksum(config)) is None
