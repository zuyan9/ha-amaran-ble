"""Exact Telink codecs for the APK's multi/Magic Pixel commands 39..42.

Commands 39, 40, and 41 come from ``MagicPixelPreConfig``,
``MultiPixelConfigProtocol``, and ``MultiPixelCfgRetryProtocol``.  Their field
names and widths below are copied directly from those protocol packers.

Command 42 is shared by the APK's Magic Pixel Rainbow, Integral, Pixel Move,
and Advanced Move packers.  Every variant proves the same outer envelope:
bits 8..60 are a 53-bit variant payload, followed by two state bits, a
four-bit effect type, and a five-bit virtual group ID.  The variant classes
have many mutually incompatible inner layouts and empty ``parseData`` methods,
and no model defaults or call sites survive in the available decompilation.
This module therefore preserves that 53-bit value as ``payload_raw`` instead
of guessing units, defaults, or a variant-specific interpretation.

All builders are strict: values outside their exact wire widths raise
``ValueError`` rather than wrapping or clamping.  Bit 79 is named ``option``
after the APK constants: zero is ACK/READ and one is WRITE.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

__all__ = [
    "CMD_MULTI_PIXEL_CONFIG",
    "CMD_MULTI_PIXEL_CONFIG_RETRY",
    "CMD_MULTI_PIXEL_EFFECT",
    "CMD_MULTI_PIXEL_PRE_CONFIG",
    "MultiPixelCommand",
    "MultiPixelConfig",
    "MultiPixelConfigResult",
    "MultiPixelConfigRetry",
    "MultiPixelEffect",
    "MultiPixelEffectType",
    "MultiPixelMessage",
    "MultiPixelOption",
    "MultiPixelPreConfig",
    "build_config",
    "build_config_result",
    "build_config_retry",
    "build_effect",
    "build_pre_config",
    "decode",
    "decode_config",
    "decode_config_retry",
    "decode_effect",
    "decode_pre_config",
    "encode",
]

CMD_MULTI_PIXEL_PRE_CONFIG = 39
CMD_MULTI_PIXEL_CONFIG = 40
CMD_MULTI_PIXEL_CONFIG_RETRY = 41
CMD_MULTI_PIXEL_EFFECT = 42


class MultiPixelCommand(IntEnum):
    """Command IDs declared by ``ProtocolConstant``."""

    PRE_CONFIG = CMD_MULTI_PIXEL_PRE_CONFIG
    CONFIG = CMD_MULTI_PIXEL_CONFIG
    CONFIG_RETRY = CMD_MULTI_PIXEL_CONFIG_RETRY
    EFFECT = CMD_MULTI_PIXEL_EFFECT


class MultiPixelOption(IntEnum):
    """Bit-79 values named by the APK's protocol constants."""

    ACK_OR_READ = 0
    WRITE = 1


class MultiPixelEffectType(IntEnum):
    """Command-42 effect types declared by ``ProtocolConstant``."""

    RAINBOW = 0
    INTEGRAL = 1
    PIXEL_MOVE = 2
    ADVANCED_MOVE = 3


@dataclass(frozen=True, slots=True)
class MultiPixelPreConfig:
    """Command-39 pre-configuration packet.

    The two-bit ``config_type`` has no surviving symbolic labels or defaults.
    """

    config_type: int
    option: MultiPixelOption = MultiPixelOption.WRITE


@dataclass(frozen=True, slots=True)
class MultiPixelConfig:
    """Command-40 three-node multi-pixel configuration request.

    Pixel indices/counts and shape style remain raw integers because their
    physical units and legal application-level ranges are not documented.
    """

    group_id: int
    shape_style: int
    pixel_total: int
    node1_id: int
    pixel_start1: int
    node2_id: int
    pixel_offset2: int
    node3_id: int
    pixel_offset3: int


@dataclass(frozen=True, slots=True)
class MultiPixelConfigRetry:
    """Command-41 two-node retry configuration request."""

    group_id: int
    shape_style: int
    pixel_total: int
    node1_id: int
    pixel_start1: int
    node2_id: int
    pixel_start2: int


@dataclass(frozen=True, slots=True)
class MultiPixelConfigResult:
    """Command-40/41 ACK carrying the APK-parsed four-bit result.

    The Java parsers inspect only bits 68..71.  ``opaque_raw`` preserves bits
    8..67 byte-for-byte because the APK neither requires them to be zero nor
    assigns them meaning.
    """

    command: MultiPixelCommand
    result: int
    opaque_raw: int = 0


@dataclass(frozen=True, slots=True)
class MultiPixelEffect:
    """Common command-42 envelope with its variant payload kept raw."""

    effect_type: MultiPixelEffectType
    group_id: int
    state: int
    payload_raw: int
    option: MultiPixelOption = MultiPixelOption.WRITE


type MultiPixelMessage = (
    MultiPixelPreConfig
    | MultiPixelConfig
    | MultiPixelConfigRetry
    | MultiPixelConfigResult
    | MultiPixelEffect
)


def _uint(name: str, value: int, width: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be an integer")
    maximum = (1 << width) - 1
    if not 0 <= value <= maximum:
        raise ValueError(f"{name} must be in 0..{maximum}")
    return value


def _finalize(packet: int) -> bytes:
    payload = bytearray(packet.to_bytes(10, "little"))
    payload[0] = sum(payload[1:10]) & 0xFF
    return bytes(payload)


def _build(
    command: MultiPixelCommand,
    option: MultiPixelOption,
    *fields: tuple[int, int, int],
) -> bytes:
    packet = int(command) << 72
    packet |= int(option) << 79
    for value, start, _width in fields:
        packet |= value << start
    return _finalize(packet)


def _bits(packet: int, start: int, width: int) -> int:
    return (packet >> start) & ((1 << width) - 1)


def build_pre_config(
    config_type: int,
    *,
    option: MultiPixelOption | int = MultiPixelOption.WRITE,
) -> bytes:
    """Build command 39; only config type bits 70..71 are populated."""
    return _build(
        MultiPixelCommand.PRE_CONFIG,
        MultiPixelOption(option),
        (_uint("config_type", config_type, 2), 70, 2),
    )


def build_config(
    *,
    group_id: int,
    shape_style: int,
    pixel_total: int,
    node1_id: int,
    pixel_start1: int,
    node2_id: int,
    pixel_offset2: int,
    node3_id: int,
    pixel_offset3: int,
) -> bytes:
    """Build the exact command-40 WRITE request."""
    return _build(
        MultiPixelCommand.CONFIG,
        MultiPixelOption.WRITE,
        (_uint("pixel_offset3", pixel_offset3, 3), 8, 3),
        (_uint("node3_id", node3_id, 12), 11, 12),
        (_uint("pixel_offset2", pixel_offset2, 2), 23, 2),
        (_uint("node2_id", node2_id, 12), 25, 12),
        (_uint("pixel_start1", pixel_start1, 7), 37, 7),
        (_uint("node1_id", node1_id, 12), 44, 12),
        (_uint("pixel_total", pixel_total, 7), 56, 7),
        (_uint("shape_style", shape_style, 4), 63, 4),
        (_uint("group_id", group_id, 5), 67, 5),
    )


def build_config_retry(
    *,
    group_id: int,
    shape_style: int,
    pixel_total: int,
    node1_id: int,
    pixel_start1: int,
    node2_id: int,
    pixel_start2: int,
) -> bytes:
    """Build the exact command-41 WRITE retry request."""
    return _build(
        MultiPixelCommand.CONFIG_RETRY,
        MultiPixelOption.WRITE,
        (_uint("pixel_start2", pixel_start2, 7), 18, 7),
        (_uint("node2_id", node2_id, 12), 25, 12),
        (_uint("pixel_start1", pixel_start1, 7), 37, 7),
        (_uint("node1_id", node1_id, 12), 44, 12),
        (_uint("pixel_total", pixel_total, 7), 56, 7),
        (_uint("shape_style", shape_style, 4), 63, 4),
        (_uint("group_id", group_id, 5), 67, 5),
    )


def build_config_result(
    command: MultiPixelCommand | int,
    result: int,
    *,
    opaque_raw: int = 0,
) -> bytes:
    """Build a command-40/41 ACK while preserving its ignored lower bits."""
    selected = MultiPixelCommand(command)
    if selected not in {
        MultiPixelCommand.CONFIG,
        MultiPixelCommand.CONFIG_RETRY,
    }:
        raise ValueError("config results use command 40 or 41")
    return _build(
        selected,
        MultiPixelOption.ACK_OR_READ,
        (_uint("opaque_raw", opaque_raw, 60), 8, 60),
        (_uint("result", result, 4), 68, 4),
    )


def build_effect(
    effect_type: MultiPixelEffectType | int,
    *,
    group_id: int,
    state: int,
    payload_raw: int,
    option: MultiPixelOption | int = MultiPixelOption.WRITE,
) -> bytes:
    """Build command 42 from its proven common envelope.

    ``payload_raw`` is exactly bits 8..60.  It intentionally receives no
    unit conversion or defaulting.
    """
    selected_effect = MultiPixelEffectType(effect_type)
    return _build(
        MultiPixelCommand.EFFECT,
        MultiPixelOption(option),
        (_uint("payload_raw", payload_raw, 53), 8, 53),
        (_uint("state", state, 2), 61, 2),
        (int(selected_effect), 63, 4),
        (_uint("group_id", group_id, 5), 67, 5),
    )


def encode(message: object) -> bytes:
    """Encode one typed multi/Magic Pixel message."""
    if isinstance(message, MultiPixelPreConfig):
        return build_pre_config(message.config_type, option=message.option)
    if isinstance(message, MultiPixelConfig):
        return build_config(
            group_id=message.group_id,
            shape_style=message.shape_style,
            pixel_total=message.pixel_total,
            node1_id=message.node1_id,
            pixel_start1=message.pixel_start1,
            node2_id=message.node2_id,
            pixel_offset2=message.pixel_offset2,
            node3_id=message.node3_id,
            pixel_offset3=message.pixel_offset3,
        )
    if isinstance(message, MultiPixelConfigRetry):
        return build_config_retry(
            group_id=message.group_id,
            shape_style=message.shape_style,
            pixel_total=message.pixel_total,
            node1_id=message.node1_id,
            pixel_start1=message.pixel_start1,
            node2_id=message.node2_id,
            pixel_start2=message.pixel_start2,
        )
    if isinstance(message, MultiPixelConfigResult):
        return build_config_result(
            message.command,
            message.result,
            opaque_raw=message.opaque_raw,
        )
    if isinstance(message, MultiPixelEffect):
        return build_effect(
            message.effect_type,
            group_id=message.group_id,
            state=message.state,
            payload_raw=message.payload_raw,
            option=message.option,
        )
    raise TypeError(f"unsupported multi-pixel message: {type(message).__name__}")


def decode(payload: bytes) -> MultiPixelMessage | None:
    """Decode a canonical command-39..42 request or proven ACK layout."""
    if (
        not isinstance(payload, bytes)
        or len(payload) != 10
        or payload[0] != sum(payload[1:10]) & 0xFF
    ):
        return None
    try:
        command = MultiPixelCommand(payload[9] & 0x7F)
        option = MultiPixelOption(payload[9] >> 7)
    except ValueError:
        return None

    packet = int.from_bytes(payload, "little")
    if command is MultiPixelCommand.PRE_CONFIG:
        if _bits(packet, 8, 62) != 0:
            return None
        return MultiPixelPreConfig(
            config_type=_bits(packet, 70, 2),
            option=option,
        )

    if (
        command
        in {
            MultiPixelCommand.CONFIG,
            MultiPixelCommand.CONFIG_RETRY,
        }
        and option is MultiPixelOption.ACK_OR_READ
    ):
        return MultiPixelConfigResult(
            command=command,
            result=_bits(packet, 68, 4),
            opaque_raw=_bits(packet, 8, 60),
        )

    if command is MultiPixelCommand.CONFIG:
        return MultiPixelConfig(
            group_id=_bits(packet, 67, 5),
            shape_style=_bits(packet, 63, 4),
            pixel_total=_bits(packet, 56, 7),
            node1_id=_bits(packet, 44, 12),
            pixel_start1=_bits(packet, 37, 7),
            node2_id=_bits(packet, 25, 12),
            pixel_offset2=_bits(packet, 23, 2),
            node3_id=_bits(packet, 11, 12),
            pixel_offset3=_bits(packet, 8, 3),
        )

    if command is MultiPixelCommand.CONFIG_RETRY:
        if _bits(packet, 8, 10) != 0:
            return None
        return MultiPixelConfigRetry(
            group_id=_bits(packet, 67, 5),
            shape_style=_bits(packet, 63, 4),
            pixel_total=_bits(packet, 56, 7),
            node1_id=_bits(packet, 44, 12),
            pixel_start1=_bits(packet, 37, 7),
            node2_id=_bits(packet, 25, 12),
            pixel_start2=_bits(packet, 18, 7),
        )

    try:
        effect_type = MultiPixelEffectType(_bits(packet, 63, 4))
    except ValueError:
        return None
    return MultiPixelEffect(
        effect_type=effect_type,
        group_id=_bits(packet, 67, 5),
        state=_bits(packet, 61, 2),
        payload_raw=_bits(packet, 8, 53),
        option=option,
    )


def decode_pre_config(payload: bytes) -> MultiPixelPreConfig | None:
    """Decode command 39 and reject every other family member."""
    message = decode(payload)
    return message if isinstance(message, MultiPixelPreConfig) else None


def decode_config(
    payload: bytes,
) -> MultiPixelConfig | MultiPixelConfigResult | None:
    """Decode command 40 request or result ACK."""
    message = decode(payload)
    if isinstance(message, MultiPixelConfig):
        return message
    if (
        isinstance(message, MultiPixelConfigResult)
        and message.command is MultiPixelCommand.CONFIG
    ):
        return message
    return None


def decode_config_retry(
    payload: bytes,
) -> MultiPixelConfigRetry | MultiPixelConfigResult | None:
    """Decode command 41 retry request or result ACK."""
    message = decode(payload)
    if isinstance(message, MultiPixelConfigRetry):
        return message
    if (
        isinstance(message, MultiPixelConfigResult)
        and message.command is MultiPixelCommand.CONFIG_RETRY
    ):
        return message
    return None


def decode_effect(payload: bytes) -> MultiPixelEffect | None:
    """Decode command 42's proven common envelope."""
    message = decode(payload)
    return message if isinstance(message, MultiPixelEffect) else None
