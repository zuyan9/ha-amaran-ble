"""Command-34 system effects used by newer amaran and Aputure fixtures.

The Sidus Link APK calls these ``*Protocol2`` and ``*Protocol3`` messages.
Both generations share command ``34`` but use distinct effect IDs.  All
messages are ten-byte, LSB-first Telink payloads carried by proprietary opcode
``0x26``.

Generation-II app defaults and unit conversions are taken from the APK's
``*IIEffect`` models.  The APK contains exact protocol layouts for generation
III, but no corresponding default model; those packets therefore require the
explicit :func:`effect2_packet` API instead of guessed defaults.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from enum import StrEnum
from types import MappingProxyType

CMD_SYSTEM_EFFECT_2 = 0x22


class SystemEffect2(StrEnum):
    """Second- and third-generation effects carried by command 34."""

    PAPARAZZI_II = "Paparazzi II"
    LIGHTNING_II = "Lightning II"
    TV_II = "TV II"
    FIRE_II = "Fire II"
    STROBE_II = "Strobe II"
    EXPLOSION_II = "Explosion II"
    FAULTY_BULB_II = "Faulty Bulb II"
    PULSING_II = "Pulsing II"
    WELDING_II = "Welding II"
    COP_CAR_II = "Cop Car II"
    PARTY_LIGHTS_II = "Party Lights II"
    FIREWORKS_II = "Fireworks II"
    LIGHTNING_III = "Lightning III"
    TV_III = "TV III"
    FIRE_III = "Fire III"
    FAULTY_BULB_III = "Faulty Bulb III"
    PULSING_III = "Pulsing III"
    COP_CAR_III = "Cop Car III"


SYSTEM_EFFECT2_IDS: Mapping[SystemEffect2, int] = MappingProxyType(
    {
        SystemEffect2.PAPARAZZI_II: 0,
        SystemEffect2.LIGHTNING_II: 1,
        SystemEffect2.TV_II: 2,
        SystemEffect2.FIRE_II: 3,
        SystemEffect2.STROBE_II: 4,
        SystemEffect2.EXPLOSION_II: 5,
        SystemEffect2.FAULTY_BULB_II: 6,
        SystemEffect2.PULSING_II: 7,
        SystemEffect2.WELDING_II: 8,
        SystemEffect2.COP_CAR_II: 9,
        SystemEffect2.PARTY_LIGHTS_II: 10,
        SystemEffect2.FIREWORKS_II: 11,
        SystemEffect2.LIGHTNING_III: 12,
        SystemEffect2.TV_III: 13,
        SystemEffect2.FIRE_III: 14,
        SystemEffect2.FAULTY_BULB_III: 15,
        SystemEffect2.PULSING_III: 16,
        SystemEffect2.COP_CAR_III: 17,
    }
)
_EFFECTS_BY_ID = {value: key for key, value in SYSTEM_EFFECT2_IDS.items()}

DEFAULTED_SYSTEM_EFFECT2 = frozenset(
    effect for effect, effect_id in SYSTEM_EFFECT2_IDS.items() if effect_id <= 11
)
EXPLICIT_SYSTEM_EFFECT2 = frozenset(
    effect for effect, effect_id in SYSTEM_EFFECT2_IDS.items() if effect_id >= 12
)

DUAL_PACKET_SYSTEM_EFFECT2 = frozenset(
    {
        SystemEffect2.PAPARAZZI_II,
        SystemEffect2.WELDING_II,
        SystemEffect2.LIGHTNING_III,
        SystemEffect2.TV_III,
        SystemEffect2.FIRE_III,
        SystemEffect2.FAULTY_BULB_III,
    }
)

_COMMON_COLOR_FIELDS = frozenset(
    {
        "mode",
        "kelvin",
        "gm",
        "hue",
        "saturation",
        "center_kelvin",
        "gel_kelvin",
        "gel_origin",
        "gel_type",
        "color",
    }
)
_RANGE_COLOR_FIELDS = frozenset(
    {
        "mode",
        "min_kelvin",
        "max_kelvin",
        "gm",
        "min_hue",
        "max_hue",
        "saturation",
        "center_kelvin",
    }
)

SYSTEM_EFFECT2_FIELDS: Mapping[SystemEffect2, frozenset[str]] = MappingProxyType(
    {
        SystemEffect2.PAPARAZZI_II: frozenset({"intensity", "gap_time", "min_gap_time"})
        | _COMMON_COLOR_FIELDS,
        SystemEffect2.LIGHTNING_II: frozenset({"intensity", "frequency", "speed"})
        | (_COMMON_COLOR_FIELDS - {"gel_kelvin", "gel_origin", "gel_type", "color"}),
        SystemEffect2.TV_II: frozenset({"intensity", "speed"}) | _RANGE_COLOR_FIELDS,
        SystemEffect2.FIRE_II: frozenset({"intensity", "speed"}) | _RANGE_COLOR_FIELDS,
        SystemEffect2.STROBE_II: frozenset({"intensity", "speed"})
        | _COMMON_COLOR_FIELDS,
        SystemEffect2.EXPLOSION_II: frozenset({"intensity", "decay"})
        | _COMMON_COLOR_FIELDS,
        SystemEffect2.FAULTY_BULB_II: frozenset({"intensity", "frequency", "speed"})
        | _COMMON_COLOR_FIELDS,
        SystemEffect2.PULSING_II: frozenset({"intensity", "frequency", "speed"})
        | _COMMON_COLOR_FIELDS,
        SystemEffect2.WELDING_II: frozenset({"min_intensity", "intensity", "frequency"})
        | _COMMON_COLOR_FIELDS,
        SystemEffect2.COP_CAR_II: frozenset({"intensity", "frequency", "color"}),
        SystemEffect2.PARTY_LIGHTS_II: frozenset({"intensity", "saturation", "speed"}),
        SystemEffect2.FIREWORKS_II: frozenset(
            {"intensity", "gap_time", "min_gap_time", "variant"}
        ),
        SystemEffect2.LIGHTNING_III: frozenset(
            {"intensity", "gap_time", "min_gap_time"}
        )
        | _COMMON_COLOR_FIELDS,
        SystemEffect2.TV_III: frozenset({"intensity", "gap_time", "min_gap_time"})
        | _RANGE_COLOR_FIELDS,
        SystemEffect2.FIRE_III: frozenset({"intensity", "frequency"})
        | _RANGE_COLOR_FIELDS,
        SystemEffect2.FAULTY_BULB_III: frozenset(
            {"intensity", "gap_time", "min_gap_time"}
        )
        | _COMMON_COLOR_FIELDS,
        SystemEffect2.PULSING_III: frozenset({"intensity", "frequency"})
        | _COMMON_COLOR_FIELDS,
        SystemEffect2.COP_CAR_III: frozenset({"intensity", "frequency", "color"}),
    }
)


@dataclass(frozen=True, slots=True)
class SystemEffect2State:
    """One decoded command-34 page, or a merged two-page effect state.

    Intensity uses tenths of a percent (0-1000).  Colour-temperature fields
    are returned in kelvin.  The APK represents green/magenta as a raw 0-200
    value with 100 neutral.  ``package_type`` is ``None`` for single-page or
    merged states, 0 for the timing/intensity page, and 1 for the colour page.
    """

    on: bool
    effect: SystemEffect2
    state: int
    package_type: int | None = None
    intensity: int | None = None
    frequency: int | None = None
    speed: int | None = None
    mode: int | None = None
    kelvin: int | None = None
    gm: int | None = None
    hue: int | None = None
    saturation: int | None = None
    center_kelvin: int | None = None
    min_kelvin: int | None = None
    max_kelvin: int | None = None
    min_hue: int | None = None
    max_hue: int | None = None
    min_intensity: int | None = None
    gap_time: int | None = None
    min_gap_time: int | None = None
    decay: int | None = None
    color: int | None = None
    gel_kelvin: int | None = None
    gel_origin: int | None = None
    gel_type: int | None = None
    variant: int | None = None

    @property
    def active_fields(self) -> frozenset[str]:
        """Return effect parameters present on this decoded or merged state."""
        return frozenset(
            state_field.name
            for state_field in fields(self)
            if state_field.name not in {"on", "effect", "state", "package_type"}
            and getattr(self, state_field.name) is not None
        )


def system_effect2_fields(
    system_effect: SystemEffect2 | str,
) -> frozenset[str]:
    """Return the effect-specific fields meaningful for an effect."""
    return SYSTEM_EFFECT2_FIELDS[SystemEffect2(system_effect)]


def _finalize(payload: bytearray) -> bytes:
    payload[0] = sum(payload[1:10]) & 0xFF
    return bytes(payload)


def _build_payload(
    effect: SystemEffect2,
    state: int,
    *packet_fields: tuple[int, int, int],
) -> bytes:
    packet = 0
    for value, start, width in packet_fields:
        packet |= (value & ((1 << width) - 1)) << start
    packet |= (_clamp(state, 0, 3) & 0x03) << 62
    packet |= SYSTEM_EFFECT2_IDS[effect] << 64
    packet |= CMD_SYSTEM_EFFECT_2 << 72
    packet |= 1 << 79
    return _finalize(bytearray(packet.to_bytes(10, "little")))


def _get_bits(payload: bytes, start: int, width: int) -> int:
    return (int.from_bytes(payload[:10], "little") >> start) & ((1 << width) - 1)


def _valid_payload(payload: bytes) -> bool:
    return (
        len(payload) == 10
        and payload[0] == sum(payload[1:10]) & 0xFF
        and payload[9] & 0x7F == CMD_SYSTEM_EFFECT_2
    )


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def _required(name: str, value: int | None) -> int:
    if value is None:
        raise ValueError(f"{name} is required for this packet layout")
    return value


def _kelvin_to_wire(value: int) -> int:
    return _clamp(value // 50, 0, 0x1FF)


def _color_fields(
    *,
    mode: int,
    kelvin: int | None,
    gm: int | None,
    hue: int | None,
    saturation: int | None,
    center_kelvin: int | None,
    gel_kelvin: int | None,
    gel_origin: int | None,
    gel_type: int | None,
    color: int | None,
    mode_start: int,
    cct_start: int,
    gm_start: int,
    hue_start: int,
    saturation_start: int,
    center_cct_start: int,
    gel_cct_start: int | None = None,
    gel_origin_start: int | None = None,
    gel_type_start: int | None = None,
    gel_color_start: int | None = None,
) -> tuple[tuple[int, int, int], ...]:
    selected_mode = _clamp(mode, 0, 7)
    gel_supported = None not in {
        gel_cct_start,
        gel_origin_start,
        gel_type_start,
        gel_color_start,
    }
    result: list[tuple[int, int, int]] = [(selected_mode, mode_start, 3)]
    if selected_mode == 0:
        result.extend(
            (
                (_kelvin_to_wire(_required("kelvin", kelvin)), cct_start, 9),
                (_clamp(_required("gm", gm), 0, 0xFF), gm_start, 8),
            )
        )
    elif selected_mode == 1 or not gel_supported:
        result.extend(
            (
                (_clamp(_required("hue", hue), 0, 0x1FF), hue_start, 9),
                (
                    _clamp(_required("saturation", saturation), 0, 0x7F),
                    saturation_start,
                    7,
                ),
                (
                    _kelvin_to_wire(_required("center_kelvin", center_kelvin)),
                    center_cct_start,
                    9,
                ),
            )
        )
    else:
        result.extend(
            (
                (
                    _kelvin_to_wire(_required("gel_kelvin", gel_kelvin)),
                    gel_cct_start,
                    9,
                ),
                (
                    _clamp(_required("gel_origin", gel_origin), 0, 1),
                    gel_origin_start,
                    1,
                ),
                (
                    _clamp(_required("gel_type", gel_type), 0, 0x0F),
                    gel_type_start,
                    4,
                ),
                (
                    _clamp(_required("color", color), 0, 0x3FF),
                    gel_color_start,
                    10,
                ),
            )
        )
    return tuple(result)


def _range_color_fields(
    *,
    mode: int,
    min_kelvin: int | None,
    max_kelvin: int | None,
    gm: int | None,
    min_hue: int | None,
    max_hue: int | None,
    saturation: int | None,
    center_kelvin: int | None,
    mode_start: int,
    max_start: int,
    min_start: int,
    gm_start: int,
    saturation_start: int,
    center_cct_start: int,
) -> tuple[tuple[int, int, int], ...]:
    selected_mode = _clamp(mode, 0, 7)
    result: list[tuple[int, int, int]] = [(selected_mode, mode_start, 3)]
    if selected_mode == 0:
        result.extend(
            (
                (
                    _kelvin_to_wire(_required("max_kelvin", max_kelvin)),
                    max_start,
                    9,
                ),
                (
                    _kelvin_to_wire(_required("min_kelvin", min_kelvin)),
                    min_start,
                    9,
                ),
                (_clamp(_required("gm", gm), 0, 0xFF), gm_start, 8),
            )
        )
    else:
        result.extend(
            (
                (
                    _clamp(_required("max_hue", max_hue), 0, 0x1FF),
                    max_start,
                    9,
                ),
                (
                    _clamp(_required("min_hue", min_hue), 0, 0x1FF),
                    min_start,
                    9,
                ),
                (
                    _clamp(_required("saturation", saturation), 0, 0x7F),
                    saturation_start,
                    7,
                ),
                (
                    _kelvin_to_wire(_required("center_kelvin", center_kelvin)),
                    center_cct_start,
                    9,
                ),
            )
        )
    return tuple(result)


def effect2_packet(
    system_effect: SystemEffect2 | str,
    *,
    state: int,
    package_type: int | None = None,
    intensity: int | None = None,
    frequency: int | None = None,
    speed: int | None = None,
    mode: int | None = None,
    kelvin: int | None = None,
    gm: int | None = None,
    hue: int | None = None,
    saturation: int | None = None,
    center_kelvin: int | None = None,
    min_kelvin: int | None = None,
    max_kelvin: int | None = None,
    min_hue: int | None = None,
    max_hue: int | None = None,
    min_intensity: int | None = None,
    gap_time: int | None = None,
    min_gap_time: int | None = None,
    decay: int | None = None,
    color: int | None = None,
    gel_kelvin: int | None = None,
    gel_origin: int | None = None,
    gel_type: int | None = None,
    variant: int | None = None,
) -> bytes:
    """Build one exact command-34 packet from explicit effect parameters.

    This low-level function covers all APK-proven IDs 0-17.  Dual-page
    effects require ``package_type`` 0 or 1.  Use :func:`effect2` to build the
    complete generation-II packet sequence with app defaults.
    """
    effect = SystemEffect2(system_effect)
    effect_id = SYSTEM_EFFECT2_IDS[effect]
    if effect in DUAL_PACKET_SYSTEM_EFFECT2:
        selected_package = _clamp(_required("package_type", package_type), 0, 1)
        if selected_package == 0:
            effect_intensity = _clamp(_required("intensity", intensity), 0, 1000)
            if effect in {
                SystemEffect2.PAPARAZZI_II,
                SystemEffect2.LIGHTNING_III,
                SystemEffect2.TV_III,
                SystemEffect2.FAULTY_BULB_III,
            }:
                return _build_payload(
                    effect,
                    state,
                    (effect_intensity, 51, 10),
                    (_clamp(_required("gap_time", gap_time), 0, 0x1FF), 42, 9),
                    (
                        _clamp(_required("min_gap_time", min_gap_time), 0, 0x1FF),
                        33,
                        9,
                    ),
                    (selected_package, 61, 1),
                )
            if effect is SystemEffect2.WELDING_II:
                return _build_payload(
                    effect,
                    state,
                    (effect_intensity, 51, 10),
                    (
                        _clamp(_required("min_intensity", min_intensity), 0, 1000),
                        41,
                        10,
                    ),
                    (
                        _clamp(_required("frequency", frequency), 0, 0x0F),
                        37,
                        4,
                    ),
                    (selected_package, 61, 1),
                )
            return _build_payload(
                effect,
                state,
                (effect_intensity, 51, 10),
                (
                    _clamp(_required("frequency", frequency), 0, 0xFF),
                    43,
                    8,
                ),
                (selected_package, 61, 1),
            )

        selected_mode = _required("mode", mode)
        if effect in {
            SystemEffect2.PAPARAZZI_II,
            SystemEffect2.WELDING_II,
            SystemEffect2.LIGHTNING_III,
            SystemEffect2.FAULTY_BULB_III,
        }:
            color_packet_fields = _color_fields(
                mode=selected_mode,
                kelvin=kelvin,
                gm=gm,
                hue=hue,
                saturation=saturation,
                center_kelvin=center_kelvin,
                gel_kelvin=gel_kelvin,
                gel_origin=gel_origin,
                gel_type=gel_type,
                color=color,
                mode_start=58,
                cct_start=49,
                gm_start=41,
                hue_start=49,
                saturation_start=42,
                center_cct_start=33,
                gel_cct_start=49,
                gel_origin_start=48,
                gel_type_start=44,
                gel_color_start=34,
            )
        else:
            color_packet_fields = _range_color_fields(
                mode=selected_mode,
                min_kelvin=min_kelvin,
                max_kelvin=max_kelvin,
                gm=gm,
                min_hue=min_hue,
                max_hue=max_hue,
                saturation=saturation,
                center_kelvin=center_kelvin,
                mode_start=58,
                max_start=49,
                min_start=40,
                gm_start=32,
                saturation_start=33,
                center_cct_start=24,
            )
        return _build_payload(
            effect,
            state,
            *color_packet_fields,
            (selected_package, 61, 1),
        )

    effect_intensity = _clamp(_required("intensity", intensity), 0, 1000)

    if effect in {
        SystemEffect2.LIGHTNING_II,
        SystemEffect2.FAULTY_BULB_II,
        SystemEffect2.PULSING_II,
        SystemEffect2.PULSING_III,
    }:
        frequency_width = 8 if effect is SystemEffect2.PULSING_III else 4
        frequency_start = 48 if effect is SystemEffect2.LIGHTNING_II else 44
        packet_fields: list[tuple[int, int, int]] = [
            (effect_intensity, 52, 10),
            (
                _clamp(
                    _required("frequency", frequency), 0, (1 << frequency_width) - 1
                ),
                frequency_start,
                frequency_width,
            ),
        ]
        if effect is not SystemEffect2.PULSING_III:
            packet_fields.append(
                (
                    _clamp(_required("speed", speed), 0, 0x0F),
                    44 if effect is SystemEffect2.LIGHTNING_II else 48,
                    4,
                )
            )
        packet_fields.extend(
            _color_fields(
                mode=_required("mode", mode),
                kelvin=kelvin,
                gm=gm,
                hue=hue,
                saturation=saturation,
                center_kelvin=center_kelvin,
                gel_kelvin=gel_kelvin,
                gel_origin=gel_origin,
                gel_type=gel_type,
                color=color,
                mode_start=41,
                cct_start=32,
                gm_start=24,
                hue_start=32,
                saturation_start=25,
                center_cct_start=16,
                gel_cct_start=(
                    32 if effect is not SystemEffect2.LIGHTNING_II else None
                ),
                gel_origin_start=(
                    31 if effect is not SystemEffect2.LIGHTNING_II else None
                ),
                gel_type_start=(
                    27 if effect is not SystemEffect2.LIGHTNING_II else None
                ),
                gel_color_start=(
                    17 if effect is not SystemEffect2.LIGHTNING_II else None
                ),
            )
        )
        return _build_payload(effect, state, *packet_fields)

    if effect in {SystemEffect2.TV_II, SystemEffect2.FIRE_II}:
        return _build_payload(
            effect,
            state,
            (effect_intensity, 52, 10),
            (_clamp(_required("speed", speed), 0, 0x0F), 48, 4),
            *_range_color_fields(
                mode=_required("mode", mode),
                min_kelvin=min_kelvin,
                max_kelvin=max_kelvin,
                gm=gm,
                min_hue=min_hue,
                max_hue=max_hue,
                saturation=saturation,
                center_kelvin=center_kelvin,
                mode_start=45,
                max_start=36,
                min_start=27,
                gm_start=19,
                saturation_start=20,
                center_cct_start=11,
            ),
        )

    if effect in {SystemEffect2.STROBE_II, SystemEffect2.EXPLOSION_II}:
        rate_value = speed if effect is SystemEffect2.STROBE_II else decay
        return _build_payload(
            effect,
            state,
            (effect_intensity, 52, 10),
            (_clamp(_required("speed/decay", rate_value), 0, 0x0F), 48, 4),
            *_color_fields(
                mode=_required("mode", mode),
                kelvin=kelvin,
                gm=gm,
                hue=hue,
                saturation=saturation,
                center_kelvin=center_kelvin,
                gel_kelvin=gel_kelvin,
                gel_origin=gel_origin,
                gel_type=gel_type,
                color=color,
                mode_start=45,
                cct_start=36,
                gm_start=28,
                hue_start=36,
                saturation_start=29,
                center_cct_start=20,
                gel_cct_start=36,
                gel_origin_start=35,
                gel_type_start=31,
                gel_color_start=21,
            ),
        )

    if effect in {SystemEffect2.COP_CAR_II, SystemEffect2.COP_CAR_III}:
        return _build_payload(
            effect,
            state,
            (effect_intensity, 52, 10),
            (_clamp(_required("frequency", frequency), 0, 0x0F), 48, 4),
            (_clamp(_required("color", color), 0, 0x07), 45, 3),
        )

    if effect is SystemEffect2.PARTY_LIGHTS_II:
        return _build_payload(
            effect,
            state,
            (effect_intensity, 52, 10),
            (
                _clamp(_required("saturation", saturation), 0, 0x3FF),
                42,
                10,
            ),
            (_clamp(_required("speed", speed), 0, 0x3F), 36, 6),
        )

    if effect is SystemEffect2.FIREWORKS_II:
        return _build_payload(
            effect,
            state,
            (effect_intensity, 52, 10),
            (_clamp(_required("gap_time", gap_time), 0, 0x1FF), 43, 9),
            (
                _clamp(_required("min_gap_time", min_gap_time), 0, 0x1FF),
                34,
                9,
            ),
            (_clamp(_required("variant", variant), 0, 0x03), 32, 2),
        )

    raise AssertionError(f"unhandled command-34 effect ID {effect_id}")


def effect2(
    system_effect: SystemEffect2 | str,
    *,
    on: bool = True,
    intensity: int | None = None,
    frequency: int | None = None,
    speed: int | None = None,
    mode: int | None = None,
    kelvin: int | None = None,
    gm: int | None = None,
    hue: int | None = None,
    saturation: int | None = None,
    center_kelvin: int | None = None,
    min_kelvin: int | None = None,
    max_kelvin: int | None = None,
    min_hue: int | None = None,
    max_hue: int | None = None,
    min_intensity: int | None = None,
    max_intensity: int | None = None,
    gap_time: int | None = None,
    min_gap_time: int | None = None,
    decay: int | None = None,
    color: int | None = None,
    gel_kelvin: int | None = None,
    gel_origin: int | None = None,
    gel_type: int | None = None,
    variant: int | None = None,
) -> tuple[bytes, ...]:
    """Build a complete generation-II effect sequence with APK defaults.

    A tuple is always returned because Paparazzi II and Welding II require a
    configuration page followed by a colour/state page.  Generation-III app
    defaults are not present in the APK and must be built explicitly with
    :func:`effect2_packet`.
    """
    effect = SystemEffect2(system_effect)
    if effect in EXPLICIT_SYSTEM_EFFECT2:
        raise NotImplementedError(
            f"{effect.value} has a proven wire layout but no proven app defaults; "
            "use effect2_packet with explicit fields"
        )

    state = 1 if on else 0
    regular_intensity = 180 if intensity is None else intensity
    selected_mode = 1 if mode is None else mode
    selected_kelvin = 5600 if kelvin is None else kelvin
    selected_gm = 100 if gm is None else gm
    selected_hue = 1 if hue is None else hue
    selected_saturation = 100 if saturation is None else saturation
    selected_center_kelvin = 5600 if center_kelvin is None else center_kelvin

    common_color = {
        "mode": selected_mode,
        "kelvin": selected_kelvin,
        "gm": selected_gm,
        "hue": selected_hue,
        "saturation": selected_saturation,
        "center_kelvin": selected_center_kelvin,
        "gel_kelvin": 5600 if gel_kelvin is None else gel_kelvin,
        "gel_origin": 0 if gel_origin is None else gel_origin,
        "gel_type": 0 if gel_type is None else gel_type,
        "color": 0 if color is None else color,
    }

    if effect is SystemEffect2.PAPARAZZI_II:
        maximum_gap = 60 if gap_time is None else gap_time
        minimum_gap = 20 if min_gap_time is None else min_gap_time
        return (
            effect2_packet(
                effect,
                state=3,
                package_type=0,
                intensity=regular_intensity,
                gap_time=maximum_gap,
                min_gap_time=minimum_gap,
            ),
            effect2_packet(
                effect,
                state=state,
                package_type=1,
                intensity=regular_intensity,
                **common_color,
            ),
        )

    if effect is SystemEffect2.WELDING_II:
        maximum = (
            max_intensity
            if max_intensity is not None
            else (intensity if intensity is not None else 500)
        )
        welding_center = 12 if center_kelvin is None else center_kelvin
        welding_color = dict(common_color)
        welding_color["center_kelvin"] = welding_center
        return (
            effect2_packet(
                effect,
                state=3,
                package_type=0,
                intensity=maximum,
                min_intensity=180 if min_intensity is None else min_intensity,
                frequency=5 if frequency is None else frequency,
            ),
            effect2_packet(
                effect,
                state=state,
                package_type=1,
                intensity=maximum,
                **welding_color,
            ),
        )

    if effect in {
        SystemEffect2.LIGHTNING_II,
        SystemEffect2.FAULTY_BULB_II,
        SystemEffect2.PULSING_II,
    }:
        return (
            effect2_packet(
                effect,
                state=state,
                intensity=regular_intensity,
                frequency=5 if frequency is None else frequency,
                speed=5 if speed is None else speed,
                **common_color,
            ),
        )

    if effect in {SystemEffect2.TV_II, SystemEffect2.FIRE_II}:
        return (
            effect2_packet(
                effect,
                state=state,
                intensity=regular_intensity,
                speed=5 if speed is None else speed,
                mode=selected_mode,
                min_kelvin=3200 if min_kelvin is None else min_kelvin,
                max_kelvin=6500 if max_kelvin is None else max_kelvin,
                gm=selected_gm,
                min_hue=1 if min_hue is None else min_hue,
                max_hue=180 if max_hue is None else max_hue,
                saturation=selected_saturation,
                center_kelvin=selected_center_kelvin,
            ),
        )

    if effect in {SystemEffect2.STROBE_II, SystemEffect2.EXPLOSION_II}:
        packet_options = dict(common_color)
        if effect is SystemEffect2.STROBE_II:
            packet_options["speed"] = 5 if speed is None else speed
        else:
            packet_options["decay"] = 5 if decay is None else decay
        return (
            effect2_packet(
                effect,
                state=state,
                intensity=regular_intensity,
                **packet_options,
            ),
        )

    if effect is SystemEffect2.COP_CAR_II:
        return (
            effect2_packet(
                effect,
                state=state,
                intensity=regular_intensity,
                frequency=5 if frequency is None else frequency,
                color=4 if color is None else color,
            ),
        )

    if effect is SystemEffect2.PARTY_LIGHTS_II:
        return (
            effect2_packet(
                effect,
                state=state,
                intensity=regular_intensity,
                saturation=selected_saturation,
                speed=5 if speed is None else speed,
            ),
        )

    if effect is SystemEffect2.FIREWORKS_II:
        return (
            effect2_packet(
                effect,
                state=state,
                intensity=regular_intensity,
                gap_time=60 if gap_time is None else gap_time,
                min_gap_time=20 if min_gap_time is None else min_gap_time,
                variant=0 if variant is None else variant,
            ),
        )

    raise AssertionError(f"unhandled defaulted effect {effect.value}")


def _decode_color_fields(
    payload: bytes,
    *,
    mode: int,
    cct_start: int,
    gm_start: int,
    hue_start: int,
    saturation_start: int,
    center_cct_start: int,
    gel_cct_start: int | None = None,
    gel_origin_start: int | None = None,
    gel_type_start: int | None = None,
    gel_color_start: int | None = None,
) -> dict[str, int]:
    gel_supported = None not in {
        gel_cct_start,
        gel_origin_start,
        gel_type_start,
        gel_color_start,
    }
    if mode == 0:
        return {
            "kelvin": _get_bits(payload, cct_start, 9) * 50,
            "gm": _get_bits(payload, gm_start, 8),
        }
    if mode == 1 or not gel_supported:
        return {
            "hue": _get_bits(payload, hue_start, 9),
            "saturation": _get_bits(payload, saturation_start, 7),
            "center_kelvin": _get_bits(payload, center_cct_start, 9) * 50,
        }
    return {
        "gel_kelvin": _get_bits(payload, gel_cct_start, 9) * 50,
        "gel_origin": _get_bits(payload, gel_origin_start, 1),
        "gel_type": _get_bits(payload, gel_type_start, 4),
        "color": _get_bits(payload, gel_color_start, 10),
    }


def _decode_range_color_fields(
    payload: bytes,
    *,
    mode: int,
    max_start: int,
    min_start: int,
    gm_start: int,
    saturation_start: int,
    center_cct_start: int,
) -> dict[str, int]:
    if mode == 0:
        return {
            "max_kelvin": _get_bits(payload, max_start, 9) * 50,
            "min_kelvin": _get_bits(payload, min_start, 9) * 50,
            "gm": _get_bits(payload, gm_start, 8),
        }
    return {
        "max_hue": _get_bits(payload, max_start, 9),
        "min_hue": _get_bits(payload, min_start, 9),
        "saturation": _get_bits(payload, saturation_start, 7),
        "center_kelvin": _get_bits(payload, center_cct_start, 9) * 50,
    }


def decode_effect2(payload: bytes) -> SystemEffect2State | None:
    """Decode one command-34 effect command or report page."""
    if not _valid_payload(payload):
        return None
    effect = _EFFECTS_BY_ID.get(_get_bits(payload, 64, 8))
    if effect is None:
        return None

    state = _get_bits(payload, 62, 2)
    values: dict[str, int | bool | SystemEffect2 | None] = {
        "on": state != 0,
        "effect": effect,
        "state": state,
    }

    if effect in DUAL_PACKET_SYSTEM_EFFECT2:
        package_type = _get_bits(payload, 61, 1)
        values["package_type"] = package_type
        if package_type == 0:
            values["intensity"] = _get_bits(payload, 51, 10)
            if effect in {
                SystemEffect2.PAPARAZZI_II,
                SystemEffect2.LIGHTNING_III,
                SystemEffect2.TV_III,
                SystemEffect2.FAULTY_BULB_III,
            }:
                values["gap_time"] = _get_bits(payload, 42, 9)
                values["min_gap_time"] = _get_bits(payload, 33, 9)
            elif effect is SystemEffect2.WELDING_II:
                values["min_intensity"] = _get_bits(payload, 41, 10)
                values["frequency"] = _get_bits(payload, 37, 4)
            else:
                values["frequency"] = _get_bits(payload, 43, 8)
            return SystemEffect2State(**values)  # type: ignore[arg-type]

        mode = _get_bits(payload, 58, 3)
        values["mode"] = mode
        if effect in {
            SystemEffect2.PAPARAZZI_II,
            SystemEffect2.WELDING_II,
            SystemEffect2.LIGHTNING_III,
            SystemEffect2.FAULTY_BULB_III,
        }:
            values.update(
                _decode_color_fields(
                    payload,
                    mode=mode,
                    cct_start=49,
                    gm_start=41,
                    hue_start=49,
                    saturation_start=42,
                    center_cct_start=33,
                    gel_cct_start=49,
                    gel_origin_start=48,
                    gel_type_start=44,
                    gel_color_start=34,
                )
            )
        else:
            values.update(
                _decode_range_color_fields(
                    payload,
                    mode=mode,
                    max_start=49,
                    min_start=40,
                    gm_start=32,
                    saturation_start=33,
                    center_cct_start=24,
                )
            )
        return SystemEffect2State(**values)  # type: ignore[arg-type]

    values["intensity"] = _get_bits(payload, 52, 10)
    if effect in {
        SystemEffect2.LIGHTNING_II,
        SystemEffect2.FAULTY_BULB_II,
        SystemEffect2.PULSING_II,
        SystemEffect2.PULSING_III,
    }:
        if effect is SystemEffect2.PULSING_III:
            values["frequency"] = _get_bits(payload, 44, 8)
        elif effect is SystemEffect2.LIGHTNING_II:
            values["frequency"] = _get_bits(payload, 48, 4)
            values["speed"] = _get_bits(payload, 44, 4)
        else:
            values["speed"] = _get_bits(payload, 48, 4)
            values["frequency"] = _get_bits(payload, 44, 4)
        mode = _get_bits(payload, 41, 3)
        values["mode"] = mode
        values.update(
            _decode_color_fields(
                payload,
                mode=mode,
                cct_start=32,
                gm_start=24,
                hue_start=32,
                saturation_start=25,
                center_cct_start=16,
                gel_cct_start=(
                    32 if effect is not SystemEffect2.LIGHTNING_II else None
                ),
                gel_origin_start=(
                    31 if effect is not SystemEffect2.LIGHTNING_II else None
                ),
                gel_type_start=(
                    27 if effect is not SystemEffect2.LIGHTNING_II else None
                ),
                gel_color_start=(
                    17 if effect is not SystemEffect2.LIGHTNING_II else None
                ),
            )
        )
    elif effect in {SystemEffect2.TV_II, SystemEffect2.FIRE_II}:
        values["speed"] = _get_bits(payload, 48, 4)
        mode = _get_bits(payload, 45, 3)
        values["mode"] = mode
        values.update(
            _decode_range_color_fields(
                payload,
                mode=mode,
                max_start=36,
                min_start=27,
                gm_start=19,
                saturation_start=20,
                center_cct_start=11,
            )
        )
    elif effect in {SystemEffect2.STROBE_II, SystemEffect2.EXPLOSION_II}:
        if effect is SystemEffect2.STROBE_II:
            values["speed"] = _get_bits(payload, 48, 4)
        else:
            values["decay"] = _get_bits(payload, 48, 4)
        mode = _get_bits(payload, 45, 3)
        values["mode"] = mode
        values.update(
            _decode_color_fields(
                payload,
                mode=mode,
                cct_start=36,
                gm_start=28,
                hue_start=36,
                saturation_start=29,
                center_cct_start=20,
                gel_cct_start=36,
                gel_origin_start=35,
                gel_type_start=31,
                gel_color_start=21,
            )
        )
    elif effect in {SystemEffect2.COP_CAR_II, SystemEffect2.COP_CAR_III}:
        values["frequency"] = _get_bits(payload, 48, 4)
        values["color"] = _get_bits(payload, 45, 3)
    elif effect is SystemEffect2.PARTY_LIGHTS_II:
        values["saturation"] = _get_bits(payload, 42, 10)
        values["speed"] = _get_bits(payload, 36, 6)
    elif effect is SystemEffect2.FIREWORKS_II:
        values["gap_time"] = _get_bits(payload, 43, 9)
        values["min_gap_time"] = _get_bits(payload, 34, 9)
        values["variant"] = _get_bits(payload, 32, 2)
    else:
        return None
    return SystemEffect2State(**values)  # type: ignore[arg-type]


def merge_effect2_states(
    previous: SystemEffect2State | None,
    current: SystemEffect2State,
) -> SystemEffect2State:
    """Merge the two independently reported pages of a dual-page effect."""
    if previous is None or previous.effect is not current.effect:
        return current
    if current.effect not in DUAL_PACKET_SYSTEM_EFFECT2:
        return current
    if (
        previous.package_type is not None
        and previous.package_type == current.package_type
    ):
        return current

    state_source = current if current.package_type == 1 else previous
    merged: dict[str, object] = {
        "effect": current.effect,
        "state": state_source.state,
        "on": state_source.on,
        "package_type": None,
    }
    for state_field in fields(SystemEffect2State):
        name = state_field.name
        if name in merged:
            continue
        current_value = getattr(current, name)
        merged[name] = (
            current_value if current_value is not None else getattr(previous, name)
        )
    return SystemEffect2State(**merged)  # type: ignore[arg-type]


def decode_report2(
    payload: bytes,
    previous: SystemEffect2State | None = None,
) -> SystemEffect2State | None:
    """Decode a report, optionally merging it with a cached companion page."""
    state = decode_effect2(payload)
    if state is None:
        return None
    return merge_effect2_states(previous, state)
