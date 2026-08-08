"""Raw codecs for the Sidus Link legacy (v1) partition commands.

The APK's ``PartitionColorProtocol`` (command 35),
``PartitionEffectProtocol`` (command 36), and ``PartitionConfigProtocol``
(command 38) all use the usual ten-byte Telink payload: byte zero is a checksum,
bits 72..78 carry the command, and bit 79 is the operation/literal top bit.

All public values retain their unscaled wire widths because neither units nor
meaningful UI defaults are established by these protocol classes.  Java object
fields do initially contain zero, but that is language initialization rather
than evidence of a user-facing default, so the color/effect builders require
every data field explicitly.

Two APK asymmetries are intentionally visible in this API:

* The command-35 CCT sender puts ``duvValue`` at bits 8..15 and ``cctValue`` at
  bits 16..23, while its parser assigns those names in the opposite order.
  :func:`decode_partition_color` follows the parser, so decoding a locally
  built CCT packet swaps the two named values.
* The command-36 sender writes the complement of its ``triggerMode`` field,
  while its parser returns the wire bit without complementing it.  The builder
  therefore accepts ``trigger_mode_input_raw`` and decoded state exposes both
  ``trigger_mode_wire_raw`` and its inverse property.

Command 38's sender never writes the four pixel-geometry fields, but its parser
does read them from reports.  The read/write builders consequently emit only
the proven request layouts while the decoder preserves report geometry.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import IntEnum

__all__ = [
    "CMD_PARTITION_COLOR",
    "CMD_PARTITION_CONFIG",
    "CMD_PARTITION_EFFECT",
    "PartitionColorState",
    "PartitionConfigState",
    "PartitionEffectState",
    "PartitionLightMode",
    "PartitionOperation",
    "PartitionState",
    "build_partition_color_cct",
    "build_partition_color_hsi",
    "build_partition_config_read",
    "build_partition_config_write",
    "build_partition_effect",
    "decode_partition",
    "decode_partition_color",
    "decode_partition_config",
    "decode_partition_effect",
]

CMD_PARTITION_COLOR = 35
CMD_PARTITION_EFFECT = 36
CMD_PARTITION_CONFIG = 38


class PartitionOperation(IntEnum):
    """APK-wide one-bit OPTION_READ/OPTION_WRITE discriminator."""

    READ = 0
    WRITE = 1


class PartitionLightMode(IntEnum):
    """Color representation selected by command 35's one-bit mode."""

    CCT = 0
    HSI = 1


@dataclass(frozen=True, slots=True)
class PartitionColorState:
    """Decoded command-35 color packet.

    ``indexes`` contains selected partition indexes in ascending order.  The
    mask supports indexes 0..35.  In CCT mode, ``cct_raw`` and ``duv_raw`` use
    the APK parser's names: bits 8..15 and 16..23 respectively.  That is the
    reverse of the APK sender's named CCT arguments, as described above.
    """

    light_mode: PartitionLightMode
    intensity_raw: int
    fx_state_raw: int
    indexes: tuple[int, ...]
    cct_raw: int | None = None
    duv_raw: int | None = None
    hue_raw: int | None = None
    saturation_raw: int | None = None


@dataclass(frozen=True, slots=True)
class PartitionEffectState:
    """Decoded command-36 effect packet with unscaled integer fields."""

    operation: PartitionOperation
    intensity_min_raw: int
    trigger_mode_wire_raw: int
    frequency_max_raw: int
    frequency_min_raw: int
    interval_max_raw: int
    interval_min_raw: int
    lasting_max_raw: int
    lasting_min_raw: int
    fx_mode_raw: int

    @property
    def trigger_mode_input_raw(self) -> int:
        """Return the APK sender input that reproduces the decoded wire bit."""
        return 1 - self.trigger_mode_wire_raw


@dataclass(frozen=True, slots=True)
class PartitionConfigState:
    """Decoded command-38 mode and report-only pixel geometry."""

    operation: PartitionOperation
    xy_mode_raw: int
    pixel_x1_raw: int
    pixel_y1_raw: int
    pixel_x2_raw: int
    pixel_y2_raw: int


type PartitionState = PartitionColorState | PartitionEffectState | PartitionConfigState


def _wire_value(name: str, value: int, width: int) -> int:
    if not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    maximum = (1 << width) - 1
    if not 0 <= value <= maximum:
        raise ValueError(f"{name} must be between 0 and {maximum}")
    return int(value)


def _operation(value: PartitionOperation | int) -> PartitionOperation:
    return PartitionOperation(_wire_value("operation", value, 1))


def _finalize(packet: int) -> bytes:
    payload = bytearray(packet.to_bytes(10, "little"))
    payload[0] = sum(payload[1:10]) & 0xFF
    return bytes(payload)


def _build_payload(
    command: int,
    operation: PartitionOperation,
    *fields: tuple[int, int, int],
) -> bytes:
    packet = command << 72
    packet |= int(operation) << 79
    for value, start, _width in fields:
        packet |= value << start
    return _finalize(packet)


def _decode_header(
    payload: bytes,
    command: int,
) -> tuple[int, PartitionOperation] | None:
    if (
        not isinstance(payload, bytes)
        or len(payload) != 10
        or payload[0] != sum(payload[1:10]) & 0xFF
        or payload[9] & 0x7F != command
    ):
        return None
    packet = int.from_bytes(payload, "little")
    return packet, PartitionOperation((packet >> 79) & 0x01)


def _bits(packet: int, start: int, width: int) -> int:
    return (packet >> start) & ((1 << width) - 1)


def _index_mask(indexes: Iterable[int]) -> int:
    try:
        selected = tuple(indexes)
    except TypeError as err:
        raise TypeError("indexes must be an iterable of integers") from err

    mask = 0
    seen: set[int] = set()
    for index in selected:
        if not isinstance(index, int):
            raise TypeError("partition indexes must be integers")
        if not 0 <= index <= 35:
            raise ValueError("partition indexes must be between 0 and 35")
        if index in seen:
            raise ValueError("partition indexes must not contain duplicates")
        seen.add(index)
        mask |= 1 << (35 - index)
    return mask


def _indexes(mask: int) -> tuple[int, ...]:
    return tuple(index for index in range(36) if mask & (1 << (35 - index)))


def build_partition_color_cct(
    *,
    indexes: Iterable[int],
    intensity_raw: int,
    cct_raw: int,
    duv_raw: int,
    fx_state_raw: int,
) -> bytes:
    """Build the APK's command-35 CCT write packet.

    Both color values are unscaled eight-bit sender inputs.  The app protocol
    supplies no unit conversion.  The top bit is the sender's fixed literal
    one rather than a configurable operation field.
    """
    return _build_payload(
        CMD_PARTITION_COLOR,
        PartitionOperation.WRITE,
        (_wire_value("duv_raw", duv_raw, 8), 8, 8),
        (_wire_value("cct_raw", cct_raw, 8), 16, 8),
        (_wire_value("intensity_raw", intensity_raw, 10), 24, 10),
        (PartitionLightMode.CCT, 34, 1),
        (_wire_value("fx_state_raw", fx_state_raw, 1), 35, 1),
        (_index_mask(indexes), 36, 36),
    )


def build_partition_color_hsi(
    *,
    indexes: Iterable[int],
    intensity_raw: int,
    hue_raw: int,
    saturation_raw: int,
    fx_state_raw: int,
) -> bytes:
    """Build the APK's command-35 HSI write packet from raw wire values."""
    return _build_payload(
        CMD_PARTITION_COLOR,
        PartitionOperation.WRITE,
        (_wire_value("saturation_raw", saturation_raw, 7), 8, 7),
        (_wire_value("hue_raw", hue_raw, 9), 15, 9),
        (_wire_value("intensity_raw", intensity_raw, 10), 24, 10),
        (PartitionLightMode.HSI, 34, 1),
        (_wire_value("fx_state_raw", fx_state_raw, 1), 35, 1),
        (_index_mask(indexes), 36, 36),
    )


def decode_partition_color(payload: bytes) -> PartitionColorState | None:
    """Decode a structurally valid command-35 write, otherwise return ``None``."""
    header = _decode_header(payload, CMD_PARTITION_COLOR)
    if header is None:
        return None
    packet, operation = header
    if operation is not PartitionOperation.WRITE:
        # The Java command has no operaType field; its final bit is literal one.
        return None

    light_mode = PartitionLightMode(_bits(packet, 34, 1))
    common = {
        "light_mode": light_mode,
        "intensity_raw": _bits(packet, 24, 10),
        "fx_state_raw": _bits(packet, 35, 1),
        "indexes": _indexes(_bits(packet, 36, 36)),
    }
    if light_mode is PartitionLightMode.CCT:
        # Deliberately match parseData(), whose names reverse getSendData().
        return PartitionColorState(
            **common,
            cct_raw=_bits(packet, 8, 8),
            duv_raw=_bits(packet, 16, 8),
        )
    return PartitionColorState(
        **common,
        saturation_raw=_bits(packet, 8, 7),
        hue_raw=_bits(packet, 15, 9),
    )


def build_partition_effect(
    *,
    intensity_min_raw: int,
    trigger_mode_input_raw: int,
    frequency_max_raw: int,
    frequency_min_raw: int,
    interval_max_raw: int,
    interval_min_raw: int,
    lasting_max_raw: int,
    lasting_min_raw: int,
    fx_mode_raw: int,
    operation: PartitionOperation | int,
) -> bytes:
    """Build command 36, including the APK's trigger-field complement.

    Widths are 7 bits for intensity/interval/lasting, 5 bits for frequency,
    and 2 bits for effect mode.  No physical units or cross-field ordering
    constraints are proven, so only the exact wire widths are enforced.
    """
    trigger_input = _wire_value(
        "trigger_mode_input_raw",
        trigger_mode_input_raw,
        1,
    )
    return _build_payload(
        CMD_PARTITION_EFFECT,
        _operation(operation),
        (_wire_value("intensity_min_raw", intensity_min_raw, 7), 24, 7),
        (1 - trigger_input, 31, 1),
        (_wire_value("frequency_max_raw", frequency_max_raw, 5), 32, 5),
        (_wire_value("frequency_min_raw", frequency_min_raw, 5), 37, 5),
        (_wire_value("interval_max_raw", interval_max_raw, 7), 42, 7),
        (_wire_value("interval_min_raw", interval_min_raw, 7), 49, 7),
        (_wire_value("lasting_max_raw", lasting_max_raw, 7), 56, 7),
        (_wire_value("lasting_min_raw", lasting_min_raw, 7), 63, 7),
        (_wire_value("fx_mode_raw", fx_mode_raw, 2), 70, 2),
    )


def decode_partition_effect(payload: bytes) -> PartitionEffectState | None:
    """Decode command 36 and reject non-zero reserved bits 8..23."""
    header = _decode_header(payload, CMD_PARTITION_EFFECT)
    if header is None:
        return None
    packet, operation = header
    if _bits(packet, 8, 16) != 0:
        return None
    return PartitionEffectState(
        operation=operation,
        intensity_min_raw=_bits(packet, 24, 7),
        trigger_mode_wire_raw=_bits(packet, 31, 1),
        frequency_max_raw=_bits(packet, 32, 5),
        frequency_min_raw=_bits(packet, 37, 5),
        interval_max_raw=_bits(packet, 42, 7),
        interval_min_raw=_bits(packet, 49, 7),
        lasting_max_raw=_bits(packet, 56, 7),
        lasting_min_raw=_bits(packet, 63, 7),
        fx_mode_raw=_bits(packet, 70, 2),
    )


def build_partition_config_read() -> bytes:
    """Build the no-argument command-38 read request.

    The Java constructor stores ``xyMode = -1``, but read serialization
    deliberately replaces it with a zero four-bit mode field.
    """
    return _build_payload(CMD_PARTITION_CONFIG, PartitionOperation.READ)


def build_partition_config_write(xy_mode_raw: int) -> bytes:
    """Build the command-38 write request for one raw four-bit XY mode."""
    return _build_payload(
        CMD_PARTITION_CONFIG,
        PartitionOperation.WRITE,
        (_wire_value("xy_mode_raw", xy_mode_raw, 4), 68, 4),
    )


def decode_partition_config(payload: bytes) -> PartitionConfigState | None:
    """Decode command 38 and reject non-zero reserved bits 8..47."""
    header = _decode_header(payload, CMD_PARTITION_CONFIG)
    if header is None:
        return None
    packet, operation = header
    if _bits(packet, 8, 40) != 0:
        return None
    return PartitionConfigState(
        operation=operation,
        xy_mode_raw=_bits(packet, 68, 4),
        pixel_x1_raw=_bits(packet, 64, 4),
        pixel_y1_raw=_bits(packet, 60, 4),
        pixel_x2_raw=_bits(packet, 54, 6),
        pixel_y2_raw=_bits(packet, 48, 6),
    )


def decode_partition(payload: bytes) -> PartitionState | None:
    """Dispatch a verified legacy partition payload by command number."""
    if not isinstance(payload, bytes) or len(payload) != 10:
        return None
    command = payload[9] & 0x7F
    if command == CMD_PARTITION_COLOR:
        return decode_partition_color(payload)
    if command == CMD_PARTITION_EFFECT:
        return decode_partition_effect(payload)
    if command == CMD_PARTITION_CONFIG:
        return decode_partition_config(payload)
    return None
