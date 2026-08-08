"""Strict raw codec for the APK's legacy Manual FX command 31.

The four Java protocol classes all use command 31 and identify their packet
layout with the two-bit subtype at bits 70..71:

* ``ManualEffectProtocol1`` uses subtype 0 for color and intensity ranges.
* ``ManualEffectProtocol2`` uses subtype 1 for cycle and loop parameters.
* ``ManualEffectProtocol3`` uses subtype 2 for frequency and timing fields.
* ``ManualEffectProtocol4`` uses subtype 3 for paired fade timing fields.

The APK exposes no surviving model defaults, UI bounds, unit conversions, or
symbolic values for fields such as ``ctrl``, ``effectMode``, ``seq``, ``olr``,
and ``olp``.  This module consequently keeps every value on its exact raw wire
scale.  Builders require every field used by the selected layout and reject
inactive-layout fields rather than guessing or silently discarding them.

Bit offsets use the APK's LSB-first ten-byte payload convention.  Byte zero is
the checksum, bits 72..78 contain command 31, and bit 79 is the APK-wide
ACK/READ-versus-WRITE option.  Reserved bits emitted as zero by the Java
packers are required to remain zero when decoding canonical packets.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

__all__ = [
    "CMD_MANUAL_EFFECT",
    "ManualFxMessage",
    "ManualFxOption",
    "ManualFxSubtype",
    "ManualFxSubtype0",
    "ManualFxSubtype1",
    "ManualFxSubtype2",
    "ManualFxSubtype3",
    "build_subtype0",
    "build_subtype1",
    "build_subtype2",
    "build_subtype3",
    "decode",
    "decode_subtype0",
    "decode_subtype1",
    "decode_subtype2",
    "decode_subtype3",
    "encode",
]

CMD_MANUAL_EFFECT = 31


class ManualFxOption(IntEnum):
    """Bit-79 values named by the APK's protocol constants."""

    ACK_OR_READ = 0
    WRITE = 1


class ManualFxSubtype(IntEnum):
    """Command-31 subtype values used by the four Java protocol classes."""

    COLOR_AND_INTENSITY = 0
    CYCLE_AND_LOOP = 1
    FREQUENCY_AND_TIMING = 2
    FADE_TIMING = 3


@dataclass(frozen=True, slots=True)
class ManualFxSubtype0:
    """Raw subtype-0 color and intensity range packet.

    ``base_raw == 0`` selects the GM/CCT fields.  Every other two-bit base
    value selects the saturation/hue fields, exactly matching the Java branch.
    Inactive branch fields are ``None``.
    """

    base_raw: int
    ctrl_raw: int
    intensity_seq_raw: int
    intensity_max_raw: int
    intensity_min_raw: int
    gm_seq_raw: int | None = None
    gm_max_raw: int | None = None
    gm_min_raw: int | None = None
    cct_seq_raw: int | None = None
    cct_max_raw: int | None = None
    cct_min_raw: int | None = None
    saturation_seq_raw: int | None = None
    saturation_max_raw: int | None = None
    saturation_min_raw: int | None = None
    hue_seq_raw: int | None = None
    hue_max_raw: int | None = None
    hue_min_raw: int | None = None
    option: ManualFxOption = ManualFxOption.WRITE


@dataclass(frozen=True, slots=True)
class ManualFxSubtype1:
    """Raw subtype-1 cycle and loop packet.

    Effect mode 0 uses the free-time fields, mode 1 uses fade-in fields, and
    modes 2 and 3 share the segment/overlap/OLR layout.  Inactive fields are
    ``None``.
    """

    effect_mode_raw: int
    ctrl_raw: int
    cycle_time_seq_raw: int
    cycle_time_max_raw: int
    cycle_time_min_raw: int
    loop_times_raw: int
    loop_mode_raw: int
    free_time_seq_raw: int | None = None
    free_time_max_raw: int | None = None
    free_time_min_raw: int | None = None
    fade_in_curve_raw: int | None = None
    fade_in_time_seq_raw: int | None = None
    fade_in_time_max_raw: int | None = None
    fade_in_time_min_raw: int | None = None
    unit_time_seg_raw: int | None = None
    free_time_seg_raw: int | None = None
    overlap_seq_raw: int | None = None
    olr_seq_raw: int | None = None
    olr_max_raw: int | None = None
    olr_min_raw: int | None = None
    option: ManualFxOption = ManualFxOption.WRITE


@dataclass(frozen=True, slots=True)
class ManualFxSubtype2:
    """Raw subtype-2 frequency and timing packet.

    Effect mode 0 uses frequency and unit-time ranges, mode 1 uses flicker and
    fade-out fields, and modes 2 and 3 share the OLP/unit/free-time layout.
    Inactive fields are ``None``.
    """

    effect_mode_raw: int
    ctrl_raw: int
    frequency_seq_raw: int | None = None
    frequency_max_raw: int | None = None
    frequency_min_raw: int | None = None
    unit_time_seq_raw: int | None = None
    unit_time_max_raw: int | None = None
    unit_time_min_raw: int | None = None
    flicker_frequency_raw: int | None = None
    fade_out_curve_raw: int | None = None
    fade_out_time_seq_raw: int | None = None
    fade_out_time_max_raw: int | None = None
    fade_out_time_min_raw: int | None = None
    olp_seq_raw: int | None = None
    olp_max_raw: int | None = None
    olp_min_raw: int | None = None
    free_time_max_raw: int | None = None
    free_time_min_raw: int | None = None
    option: ManualFxOption = ManualFxOption.WRITE


@dataclass(frozen=True, slots=True)
class ManualFxSubtype3:
    """Raw subtype-3 paired fade timing packet.

    Bits 66..67 are the Java class's fixed literal effect mode 2, so no
    configurable or defaulted effect-mode argument is exposed.
    """

    ctrl_raw: int
    flicker_frequency_raw: int
    fade_out_curve_raw: int
    fade_out_time_seq_raw: int
    fade_out_time_max_raw: int
    fade_out_time_min_raw: int
    fade_in_curve_raw: int
    fade_in_time_seq_raw: int
    fade_in_time_max_raw: int
    fade_in_time_min_raw: int
    option: ManualFxOption = ManualFxOption.WRITE


type ManualFxMessage = (
    ManualFxSubtype0 | ManualFxSubtype1 | ManualFxSubtype2 | ManualFxSubtype3
)


def _uint(name: str, value: int, width: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be an integer")
    maximum = (1 << width) - 1
    if not 0 <= value <= maximum:
        raise ValueError(f"{name} must be in 0..{maximum}")
    return value


def _option(value: ManualFxOption | int) -> ManualFxOption:
    return ManualFxOption(_uint("option", value, 1))


def _required(name: str, value: int | None, width: int) -> int:
    if value is None:
        raise ValueError(f"{name} is required for the selected layout")
    return _uint(name, value, width)


def _unused(**fields: int | None) -> None:
    for name, value in fields.items():
        if value is not None:
            raise ValueError(f"{name} is not used by the selected layout")


def _finalize(packet: int) -> bytes:
    payload = bytearray(packet.to_bytes(10, "little"))
    payload[0] = sum(payload[1:10]) & 0xFF
    return bytes(payload)


def _build(
    subtype: ManualFxSubtype,
    option: ManualFxOption,
    *fields: tuple[int, int, int],
) -> bytes:
    packet = CMD_MANUAL_EFFECT << 72
    packet |= int(option) << 79
    packet |= int(subtype) << 70
    for value, start, _width in fields:
        packet |= value << start
    return _finalize(packet)


def _bits(packet: int, start: int, width: int) -> int:
    return (packet >> start) & ((1 << width) - 1)


def build_subtype0(
    *,
    base_raw: int,
    ctrl_raw: int,
    intensity_seq_raw: int,
    intensity_max_raw: int,
    intensity_min_raw: int,
    gm_seq_raw: int | None = None,
    gm_max_raw: int | None = None,
    gm_min_raw: int | None = None,
    cct_seq_raw: int | None = None,
    cct_max_raw: int | None = None,
    cct_min_raw: int | None = None,
    saturation_seq_raw: int | None = None,
    saturation_max_raw: int | None = None,
    saturation_min_raw: int | None = None,
    hue_seq_raw: int | None = None,
    hue_max_raw: int | None = None,
    hue_min_raw: int | None = None,
    option: ManualFxOption | int = ManualFxOption.WRITE,
) -> bytes:
    """Build command-31 subtype 0 from raw color and intensity fields."""
    base = _uint("base_raw", base_raw, 2)
    fields: list[tuple[int, int, int]] = []
    if base == 0:
        _unused(
            saturation_seq_raw=saturation_seq_raw,
            saturation_max_raw=saturation_max_raw,
            saturation_min_raw=saturation_min_raw,
            hue_seq_raw=hue_seq_raw,
            hue_max_raw=hue_max_raw,
            hue_min_raw=hue_min_raw,
        )
        fields.extend(
            [
                (_required("gm_seq_raw", gm_seq_raw, 2), 12, 2),
                (_required("gm_max_raw", gm_max_raw, 7), 14, 7),
                (_required("gm_min_raw", gm_min_raw, 7), 21, 7),
                (_required("cct_seq_raw", cct_seq_raw, 2), 28, 2),
                (_required("cct_max_raw", cct_max_raw, 10), 30, 10),
                (_required("cct_min_raw", cct_min_raw, 10), 40, 10),
            ]
        )
    else:
        _unused(
            gm_seq_raw=gm_seq_raw,
            gm_max_raw=gm_max_raw,
            gm_min_raw=gm_min_raw,
            cct_seq_raw=cct_seq_raw,
            cct_max_raw=cct_max_raw,
            cct_min_raw=cct_min_raw,
        )
        fields.extend(
            [
                (
                    _required("saturation_seq_raw", saturation_seq_raw, 2),
                    12,
                    2,
                ),
                (
                    _required("saturation_max_raw", saturation_max_raw, 7),
                    14,
                    7,
                ),
                (
                    _required("saturation_min_raw", saturation_min_raw, 7),
                    21,
                    7,
                ),
                (_required("hue_seq_raw", hue_seq_raw, 2), 28, 2),
                (_required("hue_max_raw", hue_max_raw, 10), 30, 10),
                (_required("hue_min_raw", hue_min_raw, 10), 40, 10),
            ]
        )
    fields.extend(
        [
            (_uint("intensity_seq_raw", intensity_seq_raw, 2), 50, 2),
            (_uint("intensity_max_raw", intensity_max_raw, 7), 52, 7),
            (_uint("intensity_min_raw", intensity_min_raw, 7), 59, 7),
            (base, 66, 2),
            (_uint("ctrl_raw", ctrl_raw, 2), 68, 2),
        ]
    )
    return _build(
        ManualFxSubtype.COLOR_AND_INTENSITY,
        _option(option),
        *fields,
    )


def build_subtype1(
    *,
    effect_mode_raw: int,
    ctrl_raw: int,
    cycle_time_seq_raw: int,
    cycle_time_max_raw: int,
    cycle_time_min_raw: int,
    loop_times_raw: int,
    loop_mode_raw: int,
    free_time_seq_raw: int | None = None,
    free_time_max_raw: int | None = None,
    free_time_min_raw: int | None = None,
    fade_in_curve_raw: int | None = None,
    fade_in_time_seq_raw: int | None = None,
    fade_in_time_max_raw: int | None = None,
    fade_in_time_min_raw: int | None = None,
    unit_time_seg_raw: int | None = None,
    free_time_seg_raw: int | None = None,
    overlap_seq_raw: int | None = None,
    olr_seq_raw: int | None = None,
    olr_max_raw: int | None = None,
    olr_min_raw: int | None = None,
    option: ManualFxOption | int = ManualFxOption.WRITE,
) -> bytes:
    """Build command-31 subtype 1 from one effect-mode-specific layout."""
    effect_mode = _uint("effect_mode_raw", effect_mode_raw, 2)
    fields: list[tuple[int, int, int]] = []
    if effect_mode == 0:
        _unused(
            fade_in_curve_raw=fade_in_curve_raw,
            fade_in_time_seq_raw=fade_in_time_seq_raw,
            fade_in_time_max_raw=fade_in_time_max_raw,
            fade_in_time_min_raw=fade_in_time_min_raw,
            unit_time_seg_raw=unit_time_seg_raw,
            free_time_seg_raw=free_time_seg_raw,
            overlap_seq_raw=overlap_seq_raw,
            olr_seq_raw=olr_seq_raw,
            olr_max_raw=olr_max_raw,
            olr_min_raw=olr_min_raw,
        )
        fields.extend(
            [
                (_required("free_time_seq_raw", free_time_seq_raw, 2), 13, 2),
                (_required("free_time_max_raw", free_time_max_raw, 10), 15, 10),
                (_required("free_time_min_raw", free_time_min_raw, 10), 25, 10),
            ]
        )
    elif effect_mode == 1:
        _unused(
            free_time_seq_raw=free_time_seq_raw,
            free_time_max_raw=free_time_max_raw,
            free_time_min_raw=free_time_min_raw,
            unit_time_seg_raw=unit_time_seg_raw,
            free_time_seg_raw=free_time_seg_raw,
            overlap_seq_raw=overlap_seq_raw,
            olr_seq_raw=olr_seq_raw,
            olr_max_raw=olr_max_raw,
            olr_min_raw=olr_min_raw,
        )
        fields.extend(
            [
                (_required("fade_in_curve_raw", fade_in_curve_raw, 2), 11, 2),
                (
                    _required("fade_in_time_seq_raw", fade_in_time_seq_raw, 2),
                    13,
                    2,
                ),
                (
                    _required("fade_in_time_max_raw", fade_in_time_max_raw, 10),
                    15,
                    10,
                ),
                (
                    _required("fade_in_time_min_raw", fade_in_time_min_raw, 10),
                    25,
                    10,
                ),
            ]
        )
    else:
        _unused(
            free_time_seq_raw=free_time_seq_raw,
            free_time_max_raw=free_time_max_raw,
            free_time_min_raw=free_time_min_raw,
            fade_in_curve_raw=fade_in_curve_raw,
            fade_in_time_seq_raw=fade_in_time_seq_raw,
            fade_in_time_max_raw=fade_in_time_max_raw,
            fade_in_time_min_raw=fade_in_time_min_raw,
        )
        fields.extend(
            [
                (_required("unit_time_seg_raw", unit_time_seg_raw, 2), 13, 2),
                (_required("free_time_seg_raw", free_time_seg_raw, 2), 15, 2),
                (_required("overlap_seq_raw", overlap_seq_raw, 2), 17, 2),
                (_required("olr_seq_raw", olr_seq_raw, 2), 19, 2),
                (_required("olr_max_raw", olr_max_raw, 7), 21, 7),
                (_required("olr_min_raw", olr_min_raw, 7), 28, 7),
            ]
        )
    fields.extend(
        [
            (_uint("cycle_time_seq_raw", cycle_time_seq_raw, 2), 35, 2),
            (_uint("cycle_time_max_raw", cycle_time_max_raw, 10), 37, 10),
            (_uint("cycle_time_min_raw", cycle_time_min_raw, 10), 47, 10),
            (_uint("loop_times_raw", loop_times_raw, 7), 57, 7),
            (_uint("loop_mode_raw", loop_mode_raw, 2), 64, 2),
            (effect_mode, 66, 2),
            (_uint("ctrl_raw", ctrl_raw, 2), 68, 2),
        ]
    )
    return _build(ManualFxSubtype.CYCLE_AND_LOOP, _option(option), *fields)


def build_subtype2(
    *,
    effect_mode_raw: int,
    ctrl_raw: int,
    frequency_seq_raw: int | None = None,
    frequency_max_raw: int | None = None,
    frequency_min_raw: int | None = None,
    unit_time_seq_raw: int | None = None,
    unit_time_max_raw: int | None = None,
    unit_time_min_raw: int | None = None,
    flicker_frequency_raw: int | None = None,
    fade_out_curve_raw: int | None = None,
    fade_out_time_seq_raw: int | None = None,
    fade_out_time_max_raw: int | None = None,
    fade_out_time_min_raw: int | None = None,
    olp_seq_raw: int | None = None,
    olp_max_raw: int | None = None,
    olp_min_raw: int | None = None,
    free_time_max_raw: int | None = None,
    free_time_min_raw: int | None = None,
    option: ManualFxOption | int = ManualFxOption.WRITE,
) -> bytes:
    """Build command-31 subtype 2 from one effect-mode-specific layout."""
    effect_mode = _uint("effect_mode_raw", effect_mode_raw, 2)
    fields: list[tuple[int, int, int]] = []
    if effect_mode == 0:
        _unused(
            flicker_frequency_raw=flicker_frequency_raw,
            fade_out_curve_raw=fade_out_curve_raw,
            fade_out_time_seq_raw=fade_out_time_seq_raw,
            fade_out_time_max_raw=fade_out_time_max_raw,
            fade_out_time_min_raw=fade_out_time_min_raw,
            olp_seq_raw=olp_seq_raw,
            olp_max_raw=olp_max_raw,
            olp_min_raw=olp_min_raw,
            free_time_max_raw=free_time_max_raw,
            free_time_min_raw=free_time_min_raw,
        )
        fields.extend(
            [
                (_required("frequency_seq_raw", frequency_seq_raw, 2), 26, 2),
                (_required("frequency_max_raw", frequency_max_raw, 8), 28, 8),
                (_required("frequency_min_raw", frequency_min_raw, 8), 36, 8),
                (_required("unit_time_seq_raw", unit_time_seq_raw, 2), 44, 2),
                (_required("unit_time_max_raw", unit_time_max_raw, 10), 46, 10),
                (_required("unit_time_min_raw", unit_time_min_raw, 10), 56, 10),
            ]
        )
    elif effect_mode == 1:
        _unused(
            frequency_seq_raw=frequency_seq_raw,
            frequency_max_raw=frequency_max_raw,
            frequency_min_raw=frequency_min_raw,
            unit_time_seq_raw=unit_time_seq_raw,
            unit_time_max_raw=unit_time_max_raw,
            unit_time_min_raw=unit_time_min_raw,
            olp_seq_raw=olp_seq_raw,
            olp_max_raw=olp_max_raw,
            olp_min_raw=olp_min_raw,
            free_time_max_raw=free_time_max_raw,
            free_time_min_raw=free_time_min_raw,
        )
        fields.extend(
            [
                (
                    _required("flicker_frequency_raw", flicker_frequency_raw, 8),
                    34,
                    8,
                ),
                (_required("fade_out_curve_raw", fade_out_curve_raw, 2), 42, 2),
                (
                    _required("fade_out_time_seq_raw", fade_out_time_seq_raw, 2),
                    44,
                    2,
                ),
                (
                    _required(
                        "fade_out_time_max_raw",
                        fade_out_time_max_raw,
                        10,
                    ),
                    46,
                    10,
                ),
                (
                    _required(
                        "fade_out_time_min_raw",
                        fade_out_time_min_raw,
                        10,
                    ),
                    56,
                    10,
                ),
            ]
        )
    else:
        _unused(
            frequency_seq_raw=frequency_seq_raw,
            frequency_max_raw=frequency_max_raw,
            frequency_min_raw=frequency_min_raw,
            unit_time_seq_raw=unit_time_seq_raw,
            flicker_frequency_raw=flicker_frequency_raw,
            fade_out_curve_raw=fade_out_curve_raw,
            fade_out_time_seq_raw=fade_out_time_seq_raw,
            fade_out_time_max_raw=fade_out_time_max_raw,
            fade_out_time_min_raw=fade_out_time_min_raw,
        )
        fields.extend(
            [
                (_required("olp_seq_raw", olp_seq_raw, 2), 10, 2),
                (_required("olp_max_raw", olp_max_raw, 7), 12, 7),
                (_required("olp_min_raw", olp_min_raw, 7), 19, 7),
                (_required("unit_time_max_raw", unit_time_max_raw, 10), 26, 10),
                (_required("unit_time_min_raw", unit_time_min_raw, 10), 36, 10),
                (_required("free_time_max_raw", free_time_max_raw, 10), 46, 10),
                (_required("free_time_min_raw", free_time_min_raw, 10), 56, 10),
            ]
        )
    fields.extend(
        [
            (effect_mode, 66, 2),
            (_uint("ctrl_raw", ctrl_raw, 2), 68, 2),
        ]
    )
    return _build(
        ManualFxSubtype.FREQUENCY_AND_TIMING,
        _option(option),
        *fields,
    )


def build_subtype3(
    *,
    ctrl_raw: int,
    flicker_frequency_raw: int,
    fade_out_curve_raw: int,
    fade_out_time_seq_raw: int,
    fade_out_time_max_raw: int,
    fade_out_time_min_raw: int,
    fade_in_curve_raw: int,
    fade_in_time_seq_raw: int,
    fade_in_time_max_raw: int,
    fade_in_time_min_raw: int,
    option: ManualFxOption | int = ManualFxOption.WRITE,
) -> bytes:
    """Build command-31 subtype 3 with its fixed effect-mode literal 2."""
    return _build(
        ManualFxSubtype.FADE_TIMING,
        _option(option),
        (_uint("flicker_frequency_raw", flicker_frequency_raw, 8), 10, 8),
        (_uint("fade_out_curve_raw", fade_out_curve_raw, 2), 18, 2),
        (_uint("fade_out_time_seq_raw", fade_out_time_seq_raw, 2), 20, 2),
        (_uint("fade_out_time_max_raw", fade_out_time_max_raw, 10), 22, 10),
        (_uint("fade_out_time_min_raw", fade_out_time_min_raw, 10), 32, 10),
        (_uint("fade_in_curve_raw", fade_in_curve_raw, 2), 42, 2),
        (_uint("fade_in_time_seq_raw", fade_in_time_seq_raw, 2), 44, 2),
        (_uint("fade_in_time_max_raw", fade_in_time_max_raw, 10), 46, 10),
        (_uint("fade_in_time_min_raw", fade_in_time_min_raw, 10), 56, 10),
        (2, 66, 2),
        (_uint("ctrl_raw", ctrl_raw, 2), 68, 2),
    )


def encode(message: object) -> bytes:
    """Encode one typed Manual FX packet."""
    if isinstance(message, ManualFxSubtype0):
        return build_subtype0(
            base_raw=message.base_raw,
            ctrl_raw=message.ctrl_raw,
            intensity_seq_raw=message.intensity_seq_raw,
            intensity_max_raw=message.intensity_max_raw,
            intensity_min_raw=message.intensity_min_raw,
            gm_seq_raw=message.gm_seq_raw,
            gm_max_raw=message.gm_max_raw,
            gm_min_raw=message.gm_min_raw,
            cct_seq_raw=message.cct_seq_raw,
            cct_max_raw=message.cct_max_raw,
            cct_min_raw=message.cct_min_raw,
            saturation_seq_raw=message.saturation_seq_raw,
            saturation_max_raw=message.saturation_max_raw,
            saturation_min_raw=message.saturation_min_raw,
            hue_seq_raw=message.hue_seq_raw,
            hue_max_raw=message.hue_max_raw,
            hue_min_raw=message.hue_min_raw,
            option=message.option,
        )
    if isinstance(message, ManualFxSubtype1):
        return build_subtype1(
            effect_mode_raw=message.effect_mode_raw,
            ctrl_raw=message.ctrl_raw,
            cycle_time_seq_raw=message.cycle_time_seq_raw,
            cycle_time_max_raw=message.cycle_time_max_raw,
            cycle_time_min_raw=message.cycle_time_min_raw,
            loop_times_raw=message.loop_times_raw,
            loop_mode_raw=message.loop_mode_raw,
            free_time_seq_raw=message.free_time_seq_raw,
            free_time_max_raw=message.free_time_max_raw,
            free_time_min_raw=message.free_time_min_raw,
            fade_in_curve_raw=message.fade_in_curve_raw,
            fade_in_time_seq_raw=message.fade_in_time_seq_raw,
            fade_in_time_max_raw=message.fade_in_time_max_raw,
            fade_in_time_min_raw=message.fade_in_time_min_raw,
            unit_time_seg_raw=message.unit_time_seg_raw,
            free_time_seg_raw=message.free_time_seg_raw,
            overlap_seq_raw=message.overlap_seq_raw,
            olr_seq_raw=message.olr_seq_raw,
            olr_max_raw=message.olr_max_raw,
            olr_min_raw=message.olr_min_raw,
            option=message.option,
        )
    if isinstance(message, ManualFxSubtype2):
        return build_subtype2(
            effect_mode_raw=message.effect_mode_raw,
            ctrl_raw=message.ctrl_raw,
            frequency_seq_raw=message.frequency_seq_raw,
            frequency_max_raw=message.frequency_max_raw,
            frequency_min_raw=message.frequency_min_raw,
            unit_time_seq_raw=message.unit_time_seq_raw,
            unit_time_max_raw=message.unit_time_max_raw,
            unit_time_min_raw=message.unit_time_min_raw,
            flicker_frequency_raw=message.flicker_frequency_raw,
            fade_out_curve_raw=message.fade_out_curve_raw,
            fade_out_time_seq_raw=message.fade_out_time_seq_raw,
            fade_out_time_max_raw=message.fade_out_time_max_raw,
            fade_out_time_min_raw=message.fade_out_time_min_raw,
            olp_seq_raw=message.olp_seq_raw,
            olp_max_raw=message.olp_max_raw,
            olp_min_raw=message.olp_min_raw,
            free_time_max_raw=message.free_time_max_raw,
            free_time_min_raw=message.free_time_min_raw,
            option=message.option,
        )
    if isinstance(message, ManualFxSubtype3):
        return build_subtype3(
            ctrl_raw=message.ctrl_raw,
            flicker_frequency_raw=message.flicker_frequency_raw,
            fade_out_curve_raw=message.fade_out_curve_raw,
            fade_out_time_seq_raw=message.fade_out_time_seq_raw,
            fade_out_time_max_raw=message.fade_out_time_max_raw,
            fade_out_time_min_raw=message.fade_out_time_min_raw,
            fade_in_curve_raw=message.fade_in_curve_raw,
            fade_in_time_seq_raw=message.fade_in_time_seq_raw,
            fade_in_time_max_raw=message.fade_in_time_max_raw,
            fade_in_time_min_raw=message.fade_in_time_min_raw,
            option=message.option,
        )
    raise TypeError(f"unsupported Manual FX message: {type(message).__name__}")


def _decode_subtype0(packet: int, option: ManualFxOption) -> ManualFxSubtype0 | None:
    if _bits(packet, 8, 4) != 0:
        return None
    common = {
        "base_raw": _bits(packet, 66, 2),
        "ctrl_raw": _bits(packet, 68, 2),
        "intensity_seq_raw": _bits(packet, 50, 2),
        "intensity_max_raw": _bits(packet, 52, 7),
        "intensity_min_raw": _bits(packet, 59, 7),
        "option": option,
    }
    if common["base_raw"] == 0:
        return ManualFxSubtype0(
            **common,
            gm_seq_raw=_bits(packet, 12, 2),
            gm_max_raw=_bits(packet, 14, 7),
            gm_min_raw=_bits(packet, 21, 7),
            cct_seq_raw=_bits(packet, 28, 2),
            cct_max_raw=_bits(packet, 30, 10),
            cct_min_raw=_bits(packet, 40, 10),
        )
    return ManualFxSubtype0(
        **common,
        saturation_seq_raw=_bits(packet, 12, 2),
        saturation_max_raw=_bits(packet, 14, 7),
        saturation_min_raw=_bits(packet, 21, 7),
        hue_seq_raw=_bits(packet, 28, 2),
        hue_max_raw=_bits(packet, 30, 10),
        hue_min_raw=_bits(packet, 40, 10),
    )


def _decode_subtype1(packet: int, option: ManualFxOption) -> ManualFxSubtype1 | None:
    effect_mode = _bits(packet, 66, 2)
    reserved_width = 3 if effect_mode == 1 else 5
    if _bits(packet, 8, reserved_width) != 0:
        return None
    common = {
        "effect_mode_raw": effect_mode,
        "ctrl_raw": _bits(packet, 68, 2),
        "cycle_time_seq_raw": _bits(packet, 35, 2),
        "cycle_time_max_raw": _bits(packet, 37, 10),
        "cycle_time_min_raw": _bits(packet, 47, 10),
        "loop_times_raw": _bits(packet, 57, 7),
        "loop_mode_raw": _bits(packet, 64, 2),
        "option": option,
    }
    if effect_mode == 0:
        return ManualFxSubtype1(
            **common,
            free_time_seq_raw=_bits(packet, 13, 2),
            free_time_max_raw=_bits(packet, 15, 10),
            free_time_min_raw=_bits(packet, 25, 10),
        )
    if effect_mode == 1:
        return ManualFxSubtype1(
            **common,
            fade_in_curve_raw=_bits(packet, 11, 2),
            fade_in_time_seq_raw=_bits(packet, 13, 2),
            fade_in_time_max_raw=_bits(packet, 15, 10),
            fade_in_time_min_raw=_bits(packet, 25, 10),
        )
    return ManualFxSubtype1(
        **common,
        unit_time_seg_raw=_bits(packet, 13, 2),
        free_time_seg_raw=_bits(packet, 15, 2),
        overlap_seq_raw=_bits(packet, 17, 2),
        olr_seq_raw=_bits(packet, 19, 2),
        olr_max_raw=_bits(packet, 21, 7),
        olr_min_raw=_bits(packet, 28, 7),
    )


def _decode_subtype2(packet: int, option: ManualFxOption) -> ManualFxSubtype2 | None:
    effect_mode = _bits(packet, 66, 2)
    reserved_width = 18 if effect_mode == 0 else 26 if effect_mode == 1 else 2
    if _bits(packet, 8, reserved_width) != 0:
        return None
    common = {
        "effect_mode_raw": effect_mode,
        "ctrl_raw": _bits(packet, 68, 2),
        "option": option,
    }
    if effect_mode == 0:
        return ManualFxSubtype2(
            **common,
            frequency_seq_raw=_bits(packet, 26, 2),
            frequency_max_raw=_bits(packet, 28, 8),
            frequency_min_raw=_bits(packet, 36, 8),
            unit_time_seq_raw=_bits(packet, 44, 2),
            unit_time_max_raw=_bits(packet, 46, 10),
            unit_time_min_raw=_bits(packet, 56, 10),
        )
    if effect_mode == 1:
        return ManualFxSubtype2(
            **common,
            flicker_frequency_raw=_bits(packet, 34, 8),
            fade_out_curve_raw=_bits(packet, 42, 2),
            fade_out_time_seq_raw=_bits(packet, 44, 2),
            fade_out_time_max_raw=_bits(packet, 46, 10),
            fade_out_time_min_raw=_bits(packet, 56, 10),
        )
    return ManualFxSubtype2(
        **common,
        olp_seq_raw=_bits(packet, 10, 2),
        olp_max_raw=_bits(packet, 12, 7),
        olp_min_raw=_bits(packet, 19, 7),
        unit_time_max_raw=_bits(packet, 26, 10),
        unit_time_min_raw=_bits(packet, 36, 10),
        free_time_max_raw=_bits(packet, 46, 10),
        free_time_min_raw=_bits(packet, 56, 10),
    )


def _decode_subtype3(packet: int, option: ManualFxOption) -> ManualFxSubtype3 | None:
    if _bits(packet, 8, 2) != 0 or _bits(packet, 66, 2) != 2:
        return None
    return ManualFxSubtype3(
        ctrl_raw=_bits(packet, 68, 2),
        flicker_frequency_raw=_bits(packet, 10, 8),
        fade_out_curve_raw=_bits(packet, 18, 2),
        fade_out_time_seq_raw=_bits(packet, 20, 2),
        fade_out_time_max_raw=_bits(packet, 22, 10),
        fade_out_time_min_raw=_bits(packet, 32, 10),
        fade_in_curve_raw=_bits(packet, 42, 2),
        fade_in_time_seq_raw=_bits(packet, 44, 2),
        fade_in_time_max_raw=_bits(packet, 46, 10),
        fade_in_time_min_raw=_bits(packet, 56, 10),
        option=option,
    )


def decode(payload: bytes) -> ManualFxMessage | None:
    """Decode one canonical command-31 packet, otherwise return ``None``."""
    if (
        not isinstance(payload, bytes)
        or len(payload) != 10
        or payload[0] != sum(payload[1:10]) & 0xFF
        or payload[9] & 0x7F != CMD_MANUAL_EFFECT
    ):
        return None
    packet = int.from_bytes(payload, "little")
    option = ManualFxOption(_bits(packet, 79, 1))
    subtype = ManualFxSubtype(_bits(packet, 70, 2))
    if subtype is ManualFxSubtype.COLOR_AND_INTENSITY:
        return _decode_subtype0(packet, option)
    if subtype is ManualFxSubtype.CYCLE_AND_LOOP:
        return _decode_subtype1(packet, option)
    if subtype is ManualFxSubtype.FREQUENCY_AND_TIMING:
        return _decode_subtype2(packet, option)
    return _decode_subtype3(packet, option)


def decode_subtype0(payload: bytes) -> ManualFxSubtype0 | None:
    """Decode subtype 0 and reject other Manual FX subtypes."""
    message = decode(payload)
    return message if isinstance(message, ManualFxSubtype0) else None


def decode_subtype1(payload: bytes) -> ManualFxSubtype1 | None:
    """Decode subtype 1 and reject other Manual FX subtypes."""
    message = decode(payload)
    return message if isinstance(message, ManualFxSubtype1) else None


def decode_subtype2(payload: bytes) -> ManualFxSubtype2 | None:
    """Decode subtype 2 and reject other Manual FX subtypes."""
    message = decode(payload)
    return message if isinstance(message, ManualFxSubtype2) else None


def decode_subtype3(payload: bytes) -> ManualFxSubtype3 | None:
    """Decode subtype 3 and reject other Manual FX subtypes."""
    message = decode(payload)
    return message if isinstance(message, ManualFxSubtype3) else None
