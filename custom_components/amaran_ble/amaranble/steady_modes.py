"""Raw codecs for the app's Gel, RGBW, and CIE-XY steady modes.

These Telink payloads are fixed ten-byte, little-endian bit fields.  The wire
layouts come from ``GELProtocol`` (command 3), ``RGBWProtocol`` (command 4),
and ``XYProtocol`` (command 5) in the Sidus Link APK.  Values whose app-level
units are not unambiguous retain a ``*_raw`` name and their full wire range.

In particular, two app RGB paths scale 8-bit channels differently (one uses
ceiling and another integer truncation), while no usable Gel or XY UI call
site establishes a conversion.  This module therefore performs no guessed
colour conversion.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

__all__ = [
    "CMD_GEL",
    "CMD_RGBW",
    "CMD_XY",
    "GelOrigin",
    "GelState",
    "RGBWState",
    "SteadyModeState",
    "SteadyOperation",
    "XYState",
    "build_gel",
    "build_rgbw",
    "build_xy",
    "decode_gel",
    "decode_rgbw",
    "decode_steady_mode",
    "decode_xy",
]

CMD_GEL = 3
CMD_RGBW = 4
CMD_XY = 5


class SteadyOperation(IntEnum):
    """The APK's one-bit OPTION_READ/OPTION_WRITE discriminator."""

    READ = 0
    WRITE = 1


class GelOrigin(IntEnum):
    """Gel catalogue selected by the command-3 origin bit."""

    LEE = 0
    ROSCO = 1


@dataclass(frozen=True, slots=True)
class GelState:
    """Decoded command-3 state.

    ``intensity``, ``cct_raw``, ``gel_type``, and ``color`` are the unscaled
    integer fields carried by the packet.  Their widths are 10, 10, 4, and 10
    bits respectively.
    """

    on: bool
    operation: SteadyOperation
    intensity: int
    cct_raw: int
    origin: GelOrigin
    gel_type: int
    color: int


@dataclass(frozen=True, slots=True)
class RGBWState:
    """Decoded command-4 state with all six 10-bit values unscaled."""

    on: bool
    operation: SteadyOperation
    intensity: int
    red_raw: int
    green_raw: int
    blue_raw: int
    warm_white_raw: int
    cool_white_raw: int


@dataclass(frozen=True, slots=True)
class XYState:
    """Decoded command-5 state with raw 14-bit coordinates."""

    on: bool
    operation: SteadyOperation
    intensity: int
    x_raw: int
    y_raw: int


type SteadyModeState = GelState | RGBWState | XYState


def _wire_value(name: str, value: int, width: int) -> int:
    if not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    maximum = (1 << width) - 1
    if not 0 <= value <= maximum:
        raise ValueError(f"{name} must be between 0 and {maximum}")
    return int(value)


def _build_payload(
    command: int,
    operation: SteadyOperation | int,
    *fields: tuple[int, int, int],
) -> bytes:
    packet = 0
    for value, start, width in fields:
        packet |= (value & ((1 << width) - 1)) << start
    packet |= command << 72
    packet |= int(SteadyOperation(operation)) << 79
    payload = bytearray(packet.to_bytes(10, "little"))
    payload[0] = sum(payload[1:10]) & 0xFF
    return bytes(payload)


def _decode_header(
    payload: bytes,
    command: int,
) -> tuple[int, bool, SteadyOperation] | None:
    if (
        len(payload) != 10
        or payload[0] != sum(payload[1:10]) & 0xFF
        or payload[9] & 0x7F != command
    ):
        return None
    packet = int.from_bytes(payload, "little")
    return (
        packet,
        bool((packet >> 8) & 0x01),
        SteadyOperation((packet >> 79) & 0x01),
    )


def _bits(packet: int, start: int, width: int) -> int:
    return (packet >> start) & ((1 << width) - 1)


def build_gel(
    *,
    intensity: int,
    cct_raw: int,
    origin: GelOrigin | int,
    gel_type: int,
    color: int,
    on: bool | int = True,
    operation: SteadyOperation | int = SteadyOperation.WRITE,
) -> bytes:
    """Build command 3 using only the APK's proven raw fields."""
    return _build_payload(
        CMD_GEL,
        operation,
        (_wire_value("on", on, 1), 8, 1),
        (_wire_value("color", color, 10), 37, 10),
        (_wire_value("gel_type", gel_type, 4), 47, 4),
        (int(GelOrigin(origin)), 51, 1),
        (_wire_value("cct_raw", cct_raw, 10), 52, 10),
        (_wire_value("intensity", intensity, 10), 62, 10),
    )


def decode_gel(payload: bytes) -> GelState | None:
    """Decode a verified command-3 write or report payload."""
    header = _decode_header(payload, CMD_GEL)
    if header is None:
        return None
    packet, on, operation = header
    return GelState(
        on=on,
        operation=operation,
        intensity=_bits(packet, 62, 10),
        cct_raw=_bits(packet, 52, 10),
        origin=GelOrigin(_bits(packet, 51, 1)),
        gel_type=_bits(packet, 47, 4),
        color=_bits(packet, 37, 10),
    )


def build_rgbw(
    *,
    intensity: int,
    red_raw: int,
    green_raw: int,
    blue_raw: int,
    warm_white_raw: int = 0,
    cool_white_raw: int = 0,
    on: bool | int = True,
    operation: SteadyOperation | int = SteadyOperation.WRITE,
) -> bytes:
    """Build command 4 from six unscaled 10-bit channel values."""
    return _build_payload(
        CMD_RGBW,
        operation,
        (_wire_value("on", on, 1), 8, 1),
        (_wire_value("intensity", intensity, 10), 12, 10),
        (_wire_value("cool_white_raw", cool_white_raw, 10), 22, 10),
        (_wire_value("warm_white_raw", warm_white_raw, 10), 32, 10),
        (_wire_value("blue_raw", blue_raw, 10), 42, 10),
        (_wire_value("green_raw", green_raw, 10), 52, 10),
        (_wire_value("red_raw", red_raw, 10), 62, 10),
    )


def decode_rgbw(payload: bytes) -> RGBWState | None:
    """Decode a verified command-4 write or report payload."""
    header = _decode_header(payload, CMD_RGBW)
    if header is None:
        return None
    packet, on, operation = header
    return RGBWState(
        on=on,
        operation=operation,
        intensity=_bits(packet, 12, 10),
        red_raw=_bits(packet, 62, 10),
        green_raw=_bits(packet, 52, 10),
        blue_raw=_bits(packet, 42, 10),
        warm_white_raw=_bits(packet, 32, 10),
        cool_white_raw=_bits(packet, 22, 10),
    )


def build_xy(
    *,
    intensity: int,
    x_raw: int,
    y_raw: int,
    on: bool | int = True,
    operation: SteadyOperation | int = SteadyOperation.WRITE,
) -> bytes:
    """Build command 5 from unscaled 14-bit X and Y coordinates."""
    return _build_payload(
        CMD_XY,
        operation,
        (_wire_value("on", on, 1), 8, 1),
        (_wire_value("y_raw", y_raw, 14), 34, 14),
        (_wire_value("x_raw", x_raw, 14), 48, 14),
        (_wire_value("intensity", intensity, 10), 62, 10),
    )


def decode_xy(payload: bytes) -> XYState | None:
    """Decode a verified command-5 write or report payload."""
    header = _decode_header(payload, CMD_XY)
    if header is None:
        return None
    packet, on, operation = header
    return XYState(
        on=on,
        operation=operation,
        intensity=_bits(packet, 62, 10),
        x_raw=_bits(packet, 48, 14),
        y_raw=_bits(packet, 34, 14),
    )


def decode_steady_mode(payload: bytes) -> SteadyModeState | None:
    """Dispatch any verified command-3, command-4, or command-5 payload."""
    if len(payload) != 10:
        return None
    command = payload[9] & 0x7F
    if command == CMD_GEL:
        return decode_gel(payload)
    if command == CMD_RGBW:
        return decode_rgbw(payload)
    if command == CMD_XY:
        return decode_xy(payload)
    return None
