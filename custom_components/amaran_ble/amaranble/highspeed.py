"""Command-53 high-speed-photography state codec.

The Sidus Link APK's ``HighSpeedProtocol`` stores only a one-bit state in this
message.  Bits 8 through 70 are reserved, the state is bit 71, command 53 is
bits 72 through 78, and bit 79 is an opaque ``operaType`` value.  The app's
builder always emits zero and its parser does not establish request/report
semantics for the bit, so this codec deliberately preserves it without
assigning a protocol role.

Fixture configuration also declares ``high_speed_photography_int_min`` and
``high_speed_photography_int_max`` values.  Those are fixture-specific
brightness limits expressed as percentages; they are not fields in command
53 and therefore deliberately do not appear in this codec.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

__all__ = [
    "CMD_HIGH_SPEED",
    "HighSpeedMessage",
    "HighSpeedOperation",
    "HighSpeedState",
    "build_high_speed",
    "decode_high_speed",
]

CMD_HIGH_SPEED = 53


class HighSpeedState(IntEnum):
    """High-speed-photography state encoded by command 53."""

    OFF = 0
    ON = 1


class HighSpeedOperation(IntEnum):
    """Opaque ``operaType`` bit stored at bit 79 of command 53."""

    APP_DEFAULT = 0
    OPAQUE_1 = 1


@dataclass(frozen=True, slots=True)
class HighSpeedMessage:
    """Decoded command-53 state and its opaque operation bit."""

    state: HighSpeedState
    operation: HighSpeedOperation

    @property
    def enabled(self) -> bool:
        """Return the state as a boolean for entity-facing callers."""
        return self.state is HighSpeedState.ON


def _finalize(payload: bytearray) -> bytes:
    payload[0] = sum(payload[1:10]) & 0xFF
    return bytes(payload)


def build_high_speed(
    state: HighSpeedState | bool | int,
    *,
    operation: HighSpeedOperation | int = HighSpeedOperation.APP_DEFAULT,
) -> bytes:
    """Build an exact ten-byte command-53 payload.

    The APK's public state constructor emits ``APP_DEFAULT``.  ``OPAQUE_1`` is
    exposed so packets with the top bit set can be represented and tested
    without claiming semantics the app artifact does not prove.  Both fields
    are one-bit enums; values outside 0 and 1 raise :class:`ValueError` instead
    of being silently truncated.
    """
    wire_state = HighSpeedState(state)
    wire_operation = HighSpeedOperation(operation)
    packet = int(wire_state) << 71
    packet |= CMD_HIGH_SPEED << 72
    packet |= int(wire_operation) << 79
    return _finalize(bytearray(packet.to_bytes(10, "little")))


def decode_high_speed(payload: bytes) -> HighSpeedMessage | None:
    """Decode a valid command-53 payload, otherwise return ``None``."""
    if (
        len(payload) != 10
        or payload[0] != sum(payload[1:10]) & 0xFF
        or payload[9] & 0x7F != CMD_HIGH_SPEED
    ):
        return None

    packet = int.from_bytes(payload, "little")
    return HighSpeedMessage(
        state=HighSpeedState((packet >> 71) & 0x01),
        operation=HighSpeedOperation((packet >> 79) & 0x01),
    )
