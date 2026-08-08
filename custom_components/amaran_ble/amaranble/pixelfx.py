"""Telink command-33 codecs for the app's seven legacy pixel effects.

The command IDs, packet branches, field names, and bit widths below come from
the app's ``ColorCutProtocol``, ``ColorReplaceProtocol``,
``ColorMoveProtocol1/2/3``, ``PixelFireProtocol``, and ``RainbowProtocol``.
The corresponding ``Pixel*Effect`` models prove the defaults and complete
multi-packet send order used by :func:`effect`.

Low-level packet builders deliberately require every meaningful parameter and
clamp to the proven wire width.  ``cct_raw`` and ``hsi_cct_raw`` are the
9-bit wire values in 50-kelvin units; the app truncates kelvin with ``/ 50`` on
send and multiplies by 50 on parse.  ``gm_raw`` is the app's direct 8-bit
green/magenta value (100 is the model default).  Brightness values are tenths
of a percent.

The app UI limits intensity to 0..1000.  Fade, all Chases, and Rainbow expose
speed 1..640 cm/s; Cycle and Fire expose raw speed 1..100, displayed divided
by ten as seconds and hertz respectively.  Fade/Cycle color count is 2..10,
but the fixture's pixel-zone count supplies the runtime maximum.  Chase group
is 0..1, length is 0..2, and its direction values depend on group.  These UI
limits are narrower than some wire fields, so the raw builders preserve the
full decodable wire values rather than rewriting reports.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from types import MappingProxyType
from typing import Final

__all__ = [
    "CMD_PIXEL_EFFECT",
    "PIXEL_EFFECT_IDS",
    "PixelEffect",
    "PixelEffectState",
    "PixelLightMode",
    "PixelPacketType",
    "PixelPlayback",
    "chase",
    "color",
    "color_cycle",
    "color_fade",
    "decode",
    "effect",
    "encode",
    "pixel_fire_base",
    "pixel_fire_color",
    "pixel_fire_control",
    "rainbow",
]

CMD_PIXEL_EFFECT = 33


class PixelEffect(StrEnum):
    """The seven pixel effects declared by the app for command 33."""

    COLOR_FADE = "Color Fade"
    COLOR_CYCLE = "Color Cycle"
    ONE_PIXEL_CHASE = "One Pixel Chase"
    TWO_PIXEL_CHASE = "Two Pixel Chase"
    THREE_PIXEL_CHASE = "Three Pixel Chase"
    PIXEL_FIRE = "Pixel Fire"
    RAINBOW = "Rainbow"


class PixelPlayback(IntEnum):
    """App-declared two-bit playback state."""

    STOP = 0
    PAUSE = 1
    RUNNING = 2
    CONTINUE = 3


class PixelPacketType(IntEnum):
    """Packet discriminator used by all effects except Rainbow."""

    CONTROL = 0
    COLOR = 1
    BASE = 2


class PixelLightMode(IntEnum):
    """App-declared color representation inside color packets."""

    CCT = 0
    HSI = 1
    BLACK = 2


PIXEL_EFFECT_IDS = MappingProxyType(
    {
        PixelEffect.COLOR_FADE: 0,
        PixelEffect.COLOR_CYCLE: 1,
        PixelEffect.ONE_PIXEL_CHASE: 2,
        PixelEffect.TWO_PIXEL_CHASE: 3,
        PixelEffect.THREE_PIXEL_CHASE: 4,
        PixelEffect.PIXEL_FIRE: 5,
        PixelEffect.RAINBOW: 7,
    }
)
_EFFECTS_BY_ID = {value: key for key, value in PIXEL_EFFECT_IDS.items()}
_CHASE_EFFECTS = {
    PixelEffect.ONE_PIXEL_CHASE,
    PixelEffect.TWO_PIXEL_CHASE,
    PixelEffect.THREE_PIXEL_CHASE,
}
_COMMON_COLOR_EFFECTS = {
    PixelEffect.COLOR_FADE,
    PixelEffect.COLOR_CYCLE,
    *_CHASE_EFFECTS,
}

_DEFAULT_INTENSITY: Final = 180
_DEFAULT_KELVIN_RAW: Final = 5600 // 50
_DEFAULT_FIRE_KELVIN_RAW: Final = 3200 // 50
_DEFAULT_GM_RAW: Final = 100
_DEFAULT_SATURATION: Final = 100


@dataclass(frozen=True, slots=True)
class PixelEffectState:
    """Decoded state of one command-33 packet.

    A complete effect may span multiple packets.  ``packet_type`` is ``None``
    only for Rainbow, which has a single layout without a packet discriminator.
    Optional fields are populated only when they exist in that packet layout.
    The command/report bit is deliberately omitted: callers rebuild reports as
    write commands when updating one field, rather than relaying report frames.

    Names such as ``group``, ``direction``, ``pixel_length``, ``color_count``,
    and ``serial`` preserve the Java protocol names.  Their exact bit values
    are proven; device-specific physical meaning is intentionally not inferred.
    """

    effect: PixelEffect
    playback: PixelPlayback
    packet_type: PixelPacketType | None
    speed: int | None = None
    direction: int | None = None
    frequency: int | None = None
    color_count: int | None = None
    change_way: int | None = None
    group: int | None = None
    pixel_length: int | None = None
    serial: int | None = None
    brightness: int | None = None
    max_brightness: int | None = None
    min_brightness: int | None = None
    light_mode: PixelLightMode | None = None
    cct_raw: int | None = None
    gm_raw: int | None = None
    hue: int | None = None
    saturation: int | None = None
    hsi_cct_raw: int | None = None


def _round_half_up(value: float) -> int:
    """Match Java/Kotlin and JavaScript ``Math.round`` behavior."""
    if math.isnan(value):
        return 0
    if math.isinf(value):
        return (2**63 - 1) if value > 0 else -(2**63)
    return math.floor(value + 0.5)


def _clamp(value: float, width: int) -> int:
    return max(0, min((1 << width) - 1, _round_half_up(value)))


def _finalize(payload: bytearray) -> bytes:
    payload[0] = sum(payload[1:10]) & 0xFF
    return bytes(payload)


def _build_payload(
    effect: PixelEffect,
    playback: PixelPlayback,
    *fields: tuple[int, int, int],
) -> bytes:
    packet = 0
    for value, start, width in fields:
        packet |= (value & ((1 << width) - 1)) << start
    packet |= int(playback) << 62
    packet |= PIXEL_EFFECT_IDS[effect] << 64
    packet |= CMD_PIXEL_EFFECT << 72
    packet |= 1 << 79
    return _finalize(bytearray(packet.to_bytes(10, "little")))


def _get_bits(payload: bytes, start: int, width: int) -> int:
    return (int.from_bytes(payload, "little") >> start) & ((1 << width) - 1)


def _valid_payload(payload: bytes) -> bool:
    return (
        len(payload) == 10
        and payload[0] == sum(payload[1:10]) & 0xFF
        and payload[9] & 0x7F == CMD_PIXEL_EFFECT
    )


def _effect(value: PixelEffect | str) -> PixelEffect:
    return PixelEffect(value)


def _playback(value: PixelPlayback | int) -> PixelPlayback:
    return PixelPlayback(value)


def _mode(value: PixelLightMode | int) -> PixelLightMode:
    return PixelLightMode(value)


def _require(value: int | None, name: str) -> int:
    if value is None:
        raise ValueError(f"{name} is required for this pixel packet")
    return value


def color_fade(
    *,
    playback: PixelPlayback | int,
    color_count: float,
    direction: float,
    speed: float,
) -> bytes:
    """Build Fade control: color/direction 0..15 and speed 0..1023.

    The app UI uses 2..10 colors, direction 0 Left/1 Right, and speed
    1..640 cm/s.  Its model defaults are 2, 1, and 100 respectively.
    """
    return _build_payload(
        PixelEffect.COLOR_FADE,
        _playback(playback),
        (_clamp(speed, 10), 42, 10),
        (_clamp(direction, 4), 52, 4),
        (_clamp(color_count, 4), 56, 4),
        (PixelPacketType.CONTROL, 60, 2),
    )


def color_cycle(
    *,
    playback: PixelPlayback | int,
    color_count: float,
    direction: float,
    speed: float,
    change_way: float,
) -> bytes:
    """Build Cycle control; change-way is 0..3 and speed is 0..1023.

    The app UI uses 2..10 colors, direction 0 Left/1 Right, speed 1..100
    (shown divided by ten in seconds), and change-way 0 Transient/1 Slowly.
    Its model defaults are 2, 1, 20, and 0 respectively.
    """
    return _build_payload(
        PixelEffect.COLOR_CYCLE,
        _playback(playback),
        (_clamp(change_way, 2), 40, 2),
        (_clamp(speed, 10), 42, 10),
        (_clamp(direction, 4), 52, 4),
        (_clamp(color_count, 4), 56, 4),
        (PixelPacketType.CONTROL, 60, 2),
    )


def chase(
    effect: PixelEffect | str,
    *,
    playback: PixelPlayback | int,
    group: float,
    direction: float,
    speed: float,
    pixel_length: float,
) -> bytes:
    """Build Chase control; group/direction are two-bit raw fields.

    The app exposes group 0 Single/1 Double and length 0 Short/1 Middle/2
    Long.  For Single, direction is 0 Left/1 Right/2 Loop; for Double it is
    0 Crossover/1 Rebound.  Speed is 1..640 cm/s.  The shared model defaults
    are group 0, direction 1, speed 100, and length 1.
    """
    selected = _effect(effect)
    if selected not in _CHASE_EFFECTS:
        raise ValueError(f"{selected.value} is not a chase effect")
    return _build_payload(
        selected,
        _playback(playback),
        (_clamp(speed, 10), 43, 10),
        (_clamp(pixel_length, 3), 53, 3),
        (_clamp(direction, 2), 56, 2),
        (_clamp(group, 2), 58, 2),
        (PixelPacketType.CONTROL, 60, 2),
    )


def pixel_fire_control(
    *,
    playback: PixelPlayback | int,
    frequency: float,
    direction: float,
) -> bytes:
    """Build Fire control: direction 0..3 and frequency 0..1023.

    The app calls ``frequency`` speed, limits it to 1..100, and displays it
    divided by ten in hertz.  Direction 0 is Horizontal and 1 is Vertical;
    model defaults are 20 and 0.
    """
    return _build_payload(
        PixelEffect.PIXEL_FIRE,
        _playback(playback),
        (_clamp(direction, 2), 48, 2),
        (_clamp(frequency, 10), 50, 10),
        (PixelPacketType.CONTROL, 60, 2),
    )


def rainbow(
    *,
    playback: PixelPlayback | int,
    brightness: float,
    direction: float,
    speed: float,
) -> bytes:
    """Build Rainbow: direction 0..7, brightness/speed 0..1023.

    The app limits brightness to 0..1000, speed to 1..640 cm/s, and direction
    to 0 Right/1 Left.  Model defaults are 180, 100, and 0 respectively.
    """
    return _build_payload(
        PixelEffect.RAINBOW,
        _playback(playback),
        (_clamp(speed, 10), 39, 10),
        (_clamp(direction, 3), 49, 3),
        (_clamp(brightness, 10), 52, 10),
    )


def _color_fields(
    *,
    mode: PixelLightMode,
    cct_raw: float | None,
    gm_raw: float | None,
    hue: float | None,
    saturation: float | None,
    hsi_cct_raw: float | None,
    cct_start: int,
    gm_start: int,
    hue_start: int,
    saturation_start: int,
    hsi_cct_start: int,
) -> tuple[tuple[int, int, int], ...]:
    if mode is PixelLightMode.CCT:
        if cct_raw is None or gm_raw is None:
            raise ValueError("CCT pixel colors require cct_raw and gm_raw")
        return (
            (_clamp(cct_raw, 9), cct_start, 9),
            (_clamp(gm_raw, 8), gm_start, 8),
        )
    if mode is PixelLightMode.HSI:
        if hue is None or saturation is None or hsi_cct_raw is None:
            raise ValueError(
                "HSI pixel colors require hue, saturation, and hsi_cct_raw"
            )
        return (
            (_clamp(hue, 9), hue_start, 9),
            (_clamp(saturation, 7), saturation_start, 7),
            (_clamp(hsi_cct_raw, 9), hsi_cct_start, 9),
        )
    return ()


def color(
    effect: PixelEffect | str,
    *,
    playback: PixelPlayback | int,
    serial: float,
    brightness: float,
    light_mode: PixelLightMode | int,
    cct_raw: float | None = None,
    gm_raw: float | None = None,
    hue: float | None = None,
    saturation: float | None = None,
    hsi_cct_raw: float | None = None,
) -> bytes:
    """Build a common color slot: serial 0..15, brightness 0..1023.

    CCT/HSI raw values are 9-bit, G/M is 8-bit, and saturation is 7-bit.
    """
    selected = _effect(effect)
    if selected not in _COMMON_COLOR_EFFECTS:
        raise ValueError(f"{selected.value} does not use common color packets")
    selected_mode = _mode(light_mode)
    return _build_payload(
        selected,
        _playback(playback),
        *_color_fields(
            mode=selected_mode,
            cct_raw=cct_raw,
            gm_raw=gm_raw,
            hue=hue,
            saturation=saturation,
            hsi_cct_raw=hsi_cct_raw,
            cct_start=35,
            gm_start=27,
            hue_start=35,
            saturation_start=28,
            hsi_cct_start=19,
        ),
        (selected_mode, 44, 2),
        (_clamp(brightness, 10), 46, 10),
        (_clamp(serial, 4), 56, 4),
        (PixelPacketType.COLOR, 60, 2),
    )


def pixel_fire_color(
    *,
    playback: PixelPlayback | int,
    max_brightness: float,
    min_brightness: float,
    light_mode: PixelLightMode | int,
    cct_raw: float | None = None,
    gm_raw: float | None = None,
    hue: float | None = None,
    saturation: float | None = None,
    hsi_cct_raw: float | None = None,
) -> bytes:
    """Build Fire's type-1 color packet; brightness fields are 0..1023."""
    selected_mode = _mode(light_mode)
    return _build_payload(
        PixelEffect.PIXEL_FIRE,
        _playback(playback),
        *_color_fields(
            mode=selected_mode,
            cct_raw=cct_raw,
            gm_raw=gm_raw,
            hue=hue,
            saturation=saturation,
            hsi_cct_raw=hsi_cct_raw,
            cct_start=29,
            gm_start=21,
            hue_start=29,
            saturation_start=22,
            hsi_cct_start=13,
        ),
        (selected_mode, 38, 2),
        (_clamp(min_brightness, 10), 40, 10),
        (_clamp(max_brightness, 10), 50, 10),
        (PixelPacketType.COLOR, 60, 2),
    )


def pixel_fire_base(
    *,
    playback: PixelPlayback | int,
    brightness: float,
    light_mode: PixelLightMode | int,
    cct_raw: float | None = None,
    gm_raw: float | None = None,
    hue: float | None = None,
    saturation: float | None = None,
    hsi_cct_raw: float | None = None,
) -> bytes:
    """Build Fire's type-2 base packet; brightness is 0..1023."""
    selected_mode = _mode(light_mode)
    return _build_payload(
        PixelEffect.PIXEL_FIRE,
        _playback(playback),
        *_color_fields(
            mode=selected_mode,
            cct_raw=cct_raw,
            gm_raw=gm_raw,
            hue=hue,
            saturation=saturation,
            hsi_cct_raw=hsi_cct_raw,
            cct_start=39,
            gm_start=31,
            hue_start=39,
            saturation_start=32,
            hsi_cct_start=23,
        ),
        (selected_mode, 48, 2),
        (_clamp(brightness, 10), 50, 10),
        (PixelPacketType.BASE, 60, 2),
    )


def _default_color_packet(
    selected: PixelEffect,
    serial: int,
    mode: PixelLightMode,
    *,
    hue: int = 360,
) -> bytes:
    color_values: dict[str, int] = {}
    if mode is PixelLightMode.CCT:
        color_values = {
            "cct_raw": _DEFAULT_KELVIN_RAW,
            "gm_raw": _DEFAULT_GM_RAW,
        }
    elif mode is PixelLightMode.HSI:
        color_values = {
            "hue": hue,
            "saturation": _DEFAULT_SATURATION,
            "hsi_cct_raw": _DEFAULT_KELVIN_RAW,
        }
    return color(
        selected,
        playback=PixelPlayback.CONTINUE,
        serial=serial,
        brightness=_DEFAULT_INTENSITY,
        light_mode=mode,
        **color_values,
    )


def effect(
    pixel_effect: PixelEffect | str,
    *,
    on: bool = True,
) -> tuple[bytes, ...]:
    """Build the app's complete default packet sequence for one pixel effect.

    Multi-packet effects send all colour/configuration pages with ``CONTINUE``
    first, followed by a ``RUNNING`` or ``STOP`` control page.  The effect
    models' stored default state is ``PAUSE``, but their ``buildProtocol``
    methods never send that value when entering or exiting an effect.

    This convenience API intentionally exposes only proven defaults.  Use the
    low-level builders for customized packet values.
    """
    selected = _effect(pixel_effect)
    final_playback = PixelPlayback.RUNNING if on else PixelPlayback.STOP

    if selected in {PixelEffect.COLOR_FADE, PixelEffect.COLOR_CYCLE}:
        packets = (
            _default_color_packet(selected, 0, PixelLightMode.CCT),
            _default_color_packet(selected, 1, PixelLightMode.HSI, hue=120),
        )
        if selected is PixelEffect.COLOR_FADE:
            control = color_fade(
                playback=final_playback,
                color_count=2,
                direction=1,
                speed=100,
            )
        else:
            control = color_cycle(
                playback=final_playback,
                color_count=2,
                direction=1,
                speed=20,
                change_way=0,
            )
        return (*packets, control)

    if selected in _CHASE_EFFECTS:
        default_colors = {
            PixelEffect.ONE_PIXEL_CHASE: (
                (PixelLightMode.HSI, 120),
                (PixelLightMode.CCT, 0),
            ),
            PixelEffect.TWO_PIXEL_CHASE: (
                (PixelLightMode.BLACK, 0),
                (PixelLightMode.HSI, 120),
                (PixelLightMode.HSI, 240),
            ),
            PixelEffect.THREE_PIXEL_CHASE: (
                (PixelLightMode.BLACK, 0),
                (PixelLightMode.HSI, 120),
                (PixelLightMode.HSI, 240),
                (PixelLightMode.HSI, 360),
            ),
        }[selected]
        packets = tuple(
            _default_color_packet(selected, serial, mode, hue=hue)
            for serial, (mode, hue) in enumerate(default_colors)
        )
        return (
            *packets,
            chase(
                selected,
                playback=final_playback,
                group=0,
                direction=1,
                speed=100,
                pixel_length=1,
            ),
        )

    if selected is PixelEffect.PIXEL_FIRE:
        return (
            pixel_fire_color(
                playback=PixelPlayback.CONTINUE,
                max_brightness=500,
                min_brightness=180,
                light_mode=PixelLightMode.CCT,
                cct_raw=_DEFAULT_FIRE_KELVIN_RAW,
                gm_raw=_DEFAULT_GM_RAW,
            ),
            pixel_fire_base(
                playback=PixelPlayback.CONTINUE,
                brightness=_DEFAULT_INTENSITY,
                light_mode=PixelLightMode.HSI,
                hue=360,
                saturation=_DEFAULT_SATURATION,
                hsi_cct_raw=_DEFAULT_KELVIN_RAW,
            ),
            pixel_fire_control(
                playback=final_playback,
                frequency=20,
                direction=0,
            ),
        )

    return (
        rainbow(
            playback=final_playback,
            brightness=_DEFAULT_INTENSITY,
            direction=0,
            speed=100,
        ),
    )


def encode(state: PixelEffectState) -> bytes:
    """Build a write command from one decoded packet's semantic fields.

    Both command and report frames decode to the same state. This function
    intentionally emits the app's write form, so it is not a byte-preserving
    report serializer.
    """
    effect = _effect(state.effect)
    playback = _playback(state.playback)
    if effect is PixelEffect.RAINBOW:
        if state.packet_type is not None:
            raise ValueError("Rainbow has no packet_type field")
        return rainbow(
            playback=playback,
            brightness=_require(state.brightness, "brightness"),
            direction=_require(state.direction, "direction"),
            speed=_require(state.speed, "speed"),
        )

    if state.packet_type is None:
        raise ValueError(f"{effect.value} requires packet_type")
    packet_type = PixelPacketType(state.packet_type)
    if packet_type is PixelPacketType.CONTROL:
        if effect is PixelEffect.COLOR_FADE:
            return color_fade(
                playback=playback,
                color_count=_require(state.color_count, "color_count"),
                direction=_require(state.direction, "direction"),
                speed=_require(state.speed, "speed"),
            )
        if effect is PixelEffect.COLOR_CYCLE:
            return color_cycle(
                playback=playback,
                color_count=_require(state.color_count, "color_count"),
                direction=_require(state.direction, "direction"),
                speed=_require(state.speed, "speed"),
                change_way=_require(state.change_way, "change_way"),
            )
        if effect in _CHASE_EFFECTS:
            return chase(
                effect,
                playback=playback,
                group=_require(state.group, "group"),
                direction=_require(state.direction, "direction"),
                speed=_require(state.speed, "speed"),
                pixel_length=_require(state.pixel_length, "pixel_length"),
            )
        return pixel_fire_control(
            playback=playback,
            frequency=_require(state.frequency, "frequency"),
            direction=_require(state.direction, "direction"),
        )

    color_kwargs = {
        "playback": playback,
        "light_mode": _mode(_require(state.light_mode, "light_mode")),
        "cct_raw": state.cct_raw,
        "gm_raw": state.gm_raw,
        "hue": state.hue,
        "saturation": state.saturation,
        "hsi_cct_raw": state.hsi_cct_raw,
    }
    if effect in _COMMON_COLOR_EFFECTS:
        if packet_type is not PixelPacketType.COLOR:
            raise ValueError(f"{effect.value} supports only control and color packets")
        return color(
            effect,
            serial=_require(state.serial, "serial"),
            brightness=_require(state.brightness, "brightness"),
            **color_kwargs,
        )
    if packet_type is PixelPacketType.COLOR:
        return pixel_fire_color(
            max_brightness=_require(state.max_brightness, "max_brightness"),
            min_brightness=_require(state.min_brightness, "min_brightness"),
            **color_kwargs,
        )
    if packet_type is PixelPacketType.BASE:
        return pixel_fire_base(
            brightness=_require(state.brightness, "brightness"),
            **color_kwargs,
        )
    raise ValueError("unsupported Pixel Fire packet type")


def _decode_color(
    payload: bytes,
    *,
    mode_start: int,
    cct_start: int,
    gm_start: int,
    hue_start: int,
    saturation_start: int,
    hsi_cct_start: int,
) -> dict[str, int | PixelLightMode | None] | None:
    try:
        mode = PixelLightMode(_get_bits(payload, mode_start, 2))
    except ValueError:
        return None
    result: dict[str, int | PixelLightMode | None] = {
        "light_mode": mode,
        "cct_raw": None,
        "gm_raw": None,
        "hue": None,
        "saturation": None,
        "hsi_cct_raw": None,
    }
    if mode is PixelLightMode.CCT:
        result["cct_raw"] = _get_bits(payload, cct_start, 9)
        result["gm_raw"] = _get_bits(payload, gm_start, 8)
    elif mode is PixelLightMode.HSI:
        result["hue"] = _get_bits(payload, hue_start, 9)
        result["saturation"] = _get_bits(payload, saturation_start, 7)
        result["hsi_cct_raw"] = _get_bits(payload, hsi_cct_start, 9)
    return result


def decode(payload: bytes) -> PixelEffectState | None:
    """Decode one command or report packet for the seven declared effects."""
    if not _valid_payload(payload):
        return None
    effect = _EFFECTS_BY_ID.get(_get_bits(payload, 64, 8))
    if effect is None:
        return None
    playback = PixelPlayback(_get_bits(payload, 62, 2))

    if effect is PixelEffect.RAINBOW:
        return PixelEffectState(
            effect=effect,
            playback=playback,
            packet_type=None,
            speed=_get_bits(payload, 39, 10),
            direction=_get_bits(payload, 49, 3),
            brightness=_get_bits(payload, 52, 10),
        )

    raw_packet_type = _get_bits(payload, 60, 2)
    try:
        packet_type = PixelPacketType(raw_packet_type)
    except ValueError:
        return None

    if packet_type is PixelPacketType.CONTROL:
        if effect is PixelEffect.COLOR_FADE:
            return PixelEffectState(
                effect=effect,
                playback=playback,
                packet_type=packet_type,
                speed=_get_bits(payload, 42, 10),
                direction=_get_bits(payload, 52, 4),
                color_count=_get_bits(payload, 56, 4),
            )
        if effect is PixelEffect.COLOR_CYCLE:
            return PixelEffectState(
                effect=effect,
                playback=playback,
                packet_type=packet_type,
                speed=_get_bits(payload, 42, 10),
                direction=_get_bits(payload, 52, 4),
                color_count=_get_bits(payload, 56, 4),
                change_way=_get_bits(payload, 40, 2),
            )
        if effect in _CHASE_EFFECTS:
            return PixelEffectState(
                effect=effect,
                playback=playback,
                packet_type=packet_type,
                speed=_get_bits(payload, 43, 10),
                direction=_get_bits(payload, 56, 2),
                group=_get_bits(payload, 58, 2),
                pixel_length=_get_bits(payload, 53, 3),
            )
        return PixelEffectState(
            effect=effect,
            playback=playback,
            packet_type=packet_type,
            direction=_get_bits(payload, 48, 2),
            frequency=_get_bits(payload, 50, 10),
        )

    if effect in _COMMON_COLOR_EFFECTS:
        if packet_type is not PixelPacketType.COLOR:
            return None
        color_values = _decode_color(
            payload,
            mode_start=44,
            cct_start=35,
            gm_start=27,
            hue_start=35,
            saturation_start=28,
            hsi_cct_start=19,
        )
        if color_values is None:
            return None
        return PixelEffectState(
            effect=effect,
            playback=playback,
            packet_type=packet_type,
            serial=_get_bits(payload, 56, 4),
            brightness=_get_bits(payload, 46, 10),
            **color_values,
        )

    if packet_type is PixelPacketType.COLOR:
        color_values = _decode_color(
            payload,
            mode_start=38,
            cct_start=29,
            gm_start=21,
            hue_start=29,
            saturation_start=22,
            hsi_cct_start=13,
        )
        if color_values is None:
            return None
        return PixelEffectState(
            effect=effect,
            playback=playback,
            packet_type=packet_type,
            max_brightness=_get_bits(payload, 50, 10),
            min_brightness=_get_bits(payload, 40, 10),
            **color_values,
        )
    if packet_type is PixelPacketType.BASE:
        color_values = _decode_color(
            payload,
            mode_start=48,
            cct_start=39,
            gm_start=31,
            hue_start=39,
            saturation_start=32,
            hsi_cct_start=23,
        )
        if color_values is None:
            return None
        return PixelEffectState(
            effect=effect,
            playback=playback,
            packet_type=packet_type,
            brightness=_get_bits(payload, 50, 10),
            **color_values,
        )
    return None
