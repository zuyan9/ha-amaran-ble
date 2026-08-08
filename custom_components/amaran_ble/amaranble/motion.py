"""Opaque codecs for the app-reserved motion commands 43 and 44.

The examined Sidus Link APK names command 43 ``CMD_MOTION_CONFIG`` and command
44 ``CMD_MOTION_LIVE`` and contains fixture-level motion capability gates.  It
also proves the common ten-byte framing: byte zero is the additive checksum,
bytes one through eight are command data, bits 72 through 78 are the command,
and bit 79 is ``OPTION_READ``/``OPTION_WRITE``.

That APK contains no command-43/44 parser, sender, defaults, or feature UI, so
the meanings and units of the 64 command-data bits are not available.  This
module deliberately preserves those bits as an exact eight-byte opaque value
instead of assigning speculative fields to them.  It is a transport codec,
not evidence that either command's semantic protocol is understood.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

__all__ = [
    "CMD_MOTION_CONFIG",
    "CMD_MOTION_LIVE",
    "MotionConfigMessage",
    "MotionLiveMessage",
    "MotionMessage",
    "MotionOperation",
    "build_motion_config",
    "build_motion_live",
    "decode_motion",
    "decode_motion_config",
    "decode_motion_live",
]

CMD_MOTION_CONFIG = 43
CMD_MOTION_LIVE = 44
_DATA_LENGTH = 8


class MotionOperation(IntEnum):
    """The common one-bit ``OPTION_READ``/``OPTION_WRITE`` discriminator."""

    READ = 0
    WRITE = 1


@dataclass(frozen=True, slots=True)
class MotionConfigMessage:
    """Decoded command-43 frame with uninterpreted command data."""

    data_raw: bytes
    operation: MotionOperation


@dataclass(frozen=True, slots=True)
class MotionLiveMessage:
    """Decoded command-44 frame with uninterpreted command data."""

    data_raw: bytes
    operation: MotionOperation


type MotionMessage = MotionConfigMessage | MotionLiveMessage


def _validate_data(data_raw: bytes) -> bytes:
    if not isinstance(data_raw, bytes):
        raise TypeError("data_raw must be bytes")
    if len(data_raw) != _DATA_LENGTH:
        raise ValueError("data_raw must contain exactly 8 bytes")
    return data_raw


def _build(
    command: int,
    data_raw: bytes,
    operation: MotionOperation | int,
) -> bytes:
    data = _validate_data(data_raw)
    command_byte = command | (int(MotionOperation(operation)) << 7)
    payload = bytearray(10)
    payload[1:9] = data
    payload[9] = command_byte
    payload[0] = sum(payload[1:10]) & 0xFF
    return bytes(payload)


def build_motion_config(
    data_raw: bytes,
    *,
    operation: MotionOperation | int,
) -> bytes:
    """Build command 43 without guessing the opaque data's field layout.

    ``operation`` is intentionally required because no command-43 constructor
    or call site in the examined APK establishes a safe default.
    """
    return _build(CMD_MOTION_CONFIG, data_raw, operation)


def build_motion_live(
    data_raw: bytes,
    *,
    operation: MotionOperation | int,
) -> bytes:
    """Build command 44 without guessing the opaque data's field layout.

    ``operation`` is intentionally required because no command-44 constructor
    or call site in the examined APK establishes a safe default.
    """
    return _build(CMD_MOTION_LIVE, data_raw, operation)


def _decode_header(payload: bytes, command: int) -> MotionOperation | None:
    if (
        len(payload) != 10
        or payload[0] != sum(payload[1:10]) & 0xFF
        or payload[9] & 0x7F != command
    ):
        return None
    return MotionOperation((payload[9] >> 7) & 0x01)


def decode_motion_config(payload: bytes) -> MotionConfigMessage | None:
    """Decode a valid command-43 frame, otherwise return ``None``."""
    operation = _decode_header(payload, CMD_MOTION_CONFIG)
    if operation is None:
        return None
    return MotionConfigMessage(data_raw=payload[1:9], operation=operation)


def decode_motion_live(payload: bytes) -> MotionLiveMessage | None:
    """Decode a valid command-44 frame, otherwise return ``None``."""
    operation = _decode_header(payload, CMD_MOTION_LIVE)
    if operation is None:
        return None
    return MotionLiveMessage(data_raw=payload[1:9], operation=operation)


def decode_motion(payload: bytes) -> MotionMessage | None:
    """Dispatch a valid command-43 or command-44 frame by command byte."""
    if len(payload) != 10:
        return None
    command = payload[9] & 0x7F
    if command == CMD_MOTION_CONFIG:
        return decode_motion_config(payload)
    if command == CMD_MOTION_LIVE:
        return decode_motion_live(payload)
    return None
