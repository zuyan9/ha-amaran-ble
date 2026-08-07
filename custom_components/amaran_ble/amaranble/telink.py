"""amaran's proprietary light control messages.

The fixtures expose the standard Generic OnOff and Light Lightness models and
answer them, but those models are decoupled from the physical LEDs: the actual
output is driven by a Telink-proprietary opcode ``0x26`` carrying a fixed
10-byte payload.

Ported from the amaran-BLE-control project (``src/telink.ts`` and the ESP32
firmware's ``telink.c``), which reverse-engineered the format from the amaran
desktop app.
"""

from __future__ import annotations

from dataclasses import dataclass

OPCODE = 0x26

CMD_STATUS_REQUEST = 0x0E
CMD_HSI = 0x81
CMD_CCT = 0x82
CMD_ONOFF = 0x8C
CMD_BRIGHTNESS = 0x8F

# The wire field holds kelvin/10 in 10 bits, so 10000K is the ceiling that
# round-trips. The reference implementation had a wrap encoding for higher
# values, but an Ace 25x decodes those to the wrong colour (15000K comes back
# as 5000K), so we clamp instead.
MIN_KELVIN = 800
MAX_KELVIN = 10000
MAX_INTENSITY = 1000


def _finalize(payload: bytearray) -> bytes:
    """Payload byte 0 is a checksum over bytes 1..9."""
    payload[0] = sum(payload[1:10]) & 0xFF
    return bytes(payload)


def status_request() -> bytes:
    """Ask the fixture to broadcast its current state."""
    payload = bytearray(10)
    payload[9] = CMD_STATUS_REQUEST
    return _finalize(payload)


def onoff(on: bool) -> bytes:
    payload = bytearray(10)
    payload[8] = 0x01 if on else 0x00
    payload[9] = CMD_ONOFF
    return _finalize(payload)


def brightness(intensity: float) -> bytes:
    """Intensity is 0-1000 (tenths of a percent)."""
    value = max(0, min(MAX_INTENSITY, round(intensity)))
    payload = bytearray(10)
    payload[7] = (value & 0x03) << 6
    payload[8] = (value >> 2) & 0xFF
    payload[9] = CMD_BRIGHTNESS
    return _finalize(payload)


def cct(kelvin: float, intensity: float, gm: float = 0) -> bytes:
    """Correlated colour temperature plus green/magenta shift (-10..+10)."""
    value = max(0, min(MAX_INTENSITY, round(intensity)))

    # The wire field is kelvin/10 ("telink CCT"); sending raw kelvin overflows
    # the 10-bit field. Integer-truncate to match the firmware exactly.
    tcct = int((max(MIN_KELVIN, min(MAX_KELVIN, kelvin)) + 5) // 10)

    green = max(0, min(20, round(gm) + 10))  # -10..+10 -> 0..20, neutral 10
    gm_flag = 0

    low = (value & 0x03) << 62
    high = 0x8200 | ((value >> 2) & 0xFF)
    low |= tcct << 52
    high |= (tcct >> 12) & 0xFF
    low |= (gm_flag & 0x01) << 43
    low |= (green & 0x7F) << 45

    payload = bytearray(low.to_bytes(8, "little"))
    payload.append(high & 0xFF)
    payload.append((high >> 8) & 0xFF)
    return _finalize(payload)


def hsi(hue: float, saturation: float, intensity: float) -> bytes:
    """Hue 0-360 degrees, saturation 0-100, intensity 0-1000."""
    value = max(0, min(MAX_INTENSITY, round(intensity)))
    h = max(0, min(360, round(hue))) & 0x1FF
    s = max(0, min(100, round(saturation))) & 0x7F

    payload = bytearray(10)
    payload[5] = (s & 0x03) << 6
    payload[6] = ((h & 0x07) << 5) | ((s >> 2) & 0x1F)
    payload[7] = ((h >> 3) & 0x3F) | ((value & 0x03) << 6)
    payload[8] = (value >> 2) & 0xFF
    payload[9] = CMD_HSI
    return _finalize(payload)


@dataclass(frozen=True)
class LightState:
    """A decoded status report from a fixture."""

    on: bool
    is_hsi: bool
    intensity: int  # 0-1000
    kelvin: int  # CCT mode only
    gm: int  # -10..+10, CCT mode only
    hue: int  # 0-360, HSI mode only
    saturation: int  # 0-100, HSI mode only


def decode_status(payload: bytes) -> LightState | None:
    """Decode a 10-byte status payload; ``None`` if it is not a state report.

    Fixtures also emit a ``0x0a`` diagnostic page that carries no light state.
    """
    if len(payload) < 10 or payload[0] != sum(payload[1:10]) & 0xFF:
        return None

    command = payload[9] & 0x7F  # high bit distinguishes set from report
    low = int.from_bytes(payload[:8], "little")
    high = payload[8] | (payload[9] << 8)
    on = bool((low >> 8) & 0x01)

    if command == CMD_CCT & 0x7F:
        raw = (low >> 52) & 0x3FF
        wrapped = (low >> 42) & 0x01
        telink_cct = raw + 1000 if wrapped else raw
        intensity = ((high << 2) | ((low >> 62) & 0x03)) & 0x3FF
        gm = max(-10, min(10, ((low >> 45) & 0x7F) - 10))
        return LightState(
            on=on,
            is_hsi=False,
            intensity=intensity,
            kelvin=telink_cct * 10,
            gm=gm,
            hue=0,
            saturation=0,
        )

    if command == CMD_HSI & 0x7F:
        saturation = ((payload[6] & 0x1F) << 2) | ((payload[5] >> 6) & 0x03)
        hue = ((payload[7] & 0x3F) << 3) | ((payload[6] >> 5) & 0x07)
        intensity = (payload[8] << 2) | ((payload[7] >> 6) & 0x03)
        return LightState(
            on=on,
            is_hsi=True,
            intensity=min(1000, intensity),
            kelvin=0,
            gm=0,
            hue=min(360, hue),
            saturation=min(100, saturation),
        )

    return None
