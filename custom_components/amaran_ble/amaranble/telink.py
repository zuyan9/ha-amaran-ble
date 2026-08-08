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

import math
from dataclasses import dataclass
from enum import StrEnum

OPCODE = 0x26

CMD_STATUS_REQUEST = 0x0E
CMD_HSI = 0x81
CMD_CCT = 0x82
CMD_ONOFF = 0x8C
CMD_BRIGHTNESS = 0x8F

CMD_VERSION = 0x00
CMD_SYSTEM_EFFECT = 0x07
CMD_FAN = 0x09
CMD_POWER = 0x0A
CMD_BOOST = 0x46

# The wire field stores kelvin/10. Values above 10000K wrap through the 10-bit
# field and set a separate high-range flag; see ``cct`` below.
MIN_KELVIN = 800
MAX_KELVIN = 20000
MAX_INTENSITY = 1000

ACE_MIN_KELVIN = 2700
ACE_MAX_KELVIN = 6500
ACE_BOOST_MIN_KELVIN = 3800
ACE_BOOST_MAX_KELVIN = 5500


class SystemEffect(StrEnum):
    """Ace 25x first-generation system effects."""

    OFF = "off"
    PAPARAZZI = "Paparazzi"
    FIREWORKS = "Fireworks"
    FAULTY_BULB = "Faulty Bulb"
    LIGHTNING = "Lightning"
    TV = "TV"
    PULSING = "Pulsing"
    STROBE = "Strobe"
    EXPLOSION = "Explosion"
    FIRE = "Fire"


class FanMode(StrEnum):
    """Fan modes and their user-facing Home Assistant values."""

    MANUAL = "manual"
    SMART = "smart"
    MAX = "max"
    OFF = "off"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    SILENT = "silent"


class PowerSource(StrEnum):
    """Fixture input currently supplying power."""

    BATTERY = "battery"
    EXTERNAL = "external"


_EFFECT_IDS = {
    SystemEffect.PAPARAZZI: 1,
    SystemEffect.LIGHTNING: 2,
    SystemEffect.TV: 3,
    SystemEffect.FIRE: 5,
    SystemEffect.STROBE: 6,
    SystemEffect.EXPLOSION: 7,
    SystemEffect.FAULTY_BULB: 8,
    SystemEffect.PULSING: 9,
    SystemEffect.FIREWORKS: 14,
    SystemEffect.OFF: 15,
}
_EFFECTS_BY_ID = {value: key for key, value in _EFFECT_IDS.items()}

_FAN_MODE_IDS = {
    FanMode.MANUAL: 0,
    FanMode.SMART: 1,
    FanMode.MAX: 2,
    FanMode.OFF: 3,
    FanMode.HIGH: 4,
    FanMode.MEDIUM: 5,
    FanMode.LOW: 6,
    FanMode.SILENT: 7,
}
_FAN_MODES_BY_ID = {value: key for key, value in _FAN_MODE_IDS.items()}

_FAN_SUPPORT_BITS = {
    FanMode.SILENT: 28,
    FanMode.LOW: 29,
    FanMode.MEDIUM: 30,
    FanMode.HIGH: 31,
    FanMode.OFF: 32,
    FanMode.MAX: 33,
    FanMode.SMART: 34,
    FanMode.MANUAL: 35,
}

_DEFAULT_EFFECT_TRIGGERS = {
    SystemEffect.LIGHTNING: 2,
    SystemEffect.STROBE: 2,
    SystemEffect.EXPLOSION: 1,
    SystemEffect.FAULTY_BULB: 2,
    SystemEffect.PULSING: 2,
}


def _round_half_up(value: float) -> int:
    """Match JavaScript Math.round, including exact half ties toward +infinity."""
    return math.floor(value + 0.5)


def _finalize(payload: bytearray) -> bytes:
    """Payload byte 0 is a checksum over bytes 1..9."""
    payload[0] = sum(payload[1:10]) & 0xFF
    return bytes(payload)


def _build_payload(
    command: int,
    *fields: tuple[int, int, int],
    write: bool = True,
) -> bytes:
    """Build one LSB-first 80-bit packet from ``(value, start, width)`` fields."""
    packet = 0
    for value, start, width in fields:
        packet |= (value & ((1 << width) - 1)) << start
    packet |= (command & 0x7F) << 72
    packet |= int(write) << 79
    return _finalize(bytearray(packet.to_bytes(10, "little")))


def _get_bits(payload: bytes, start: int, width: int) -> int:
    return (int.from_bytes(payload[:10], "little") >> start) & ((1 << width) - 1)


def _valid_payload(payload: bytes, command: int) -> bool:
    return (
        len(payload) == 10
        and payload[0] == sum(payload[1:10]) & 0xFF
        and payload[9] & 0x7F == command
    )


def _clamp_round(value: float, low: int, high: int) -> int:
    return max(low, min(high, _round_half_up(value)))


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
    value = max(0, min(MAX_INTENSITY, _round_half_up(intensity)))
    payload = bytearray(10)
    payload[7] = (value & 0x03) << 6
    payload[8] = (value >> 2) & 0xFF
    payload[9] = CMD_BRIGHTNESS
    return _finalize(payload)


def cct(kelvin: float, intensity: float, gm: float = 0) -> bytes:
    """Correlated colour temperature plus green/magenta shift (-10..+10)."""
    value = max(0, min(MAX_INTENSITY, _round_half_up(intensity)))

    # The wire field is kelvin/10 ("telink CCT"); sending raw kelvin overflows
    # the 10-bit field. Integer-truncate to match the firmware exactly.
    tcct = int((max(MIN_KELVIN, min(MAX_KELVIN, kelvin)) + 5) // 10)

    green = max(0, min(20, _round_half_up(gm) + 10))  # -10..+10 -> 0..20
    gm_flag = 0

    low = (value & 0x03) << 62
    high = 0x8200 | ((value >> 2) & 0xFF)
    if tcct < 1001:
        low |= tcct << 52
        high |= (tcct >> 12) & 0xFF
    else:
        # The proprietary high-CCT representation wraps 10010..20000K into
        # the 10-bit field with a +0x18 offset and marks it in bit 42.
        low |= ((tcct + 0x18) & 0x3FF) << 52
        low |= 0x0000040000000000
    low |= (gm_flag & 0x01) << 43
    low |= (green & 0x7F) << 45

    payload = bytearray(low.to_bytes(8, "little"))
    payload.append(high & 0xFF)
    payload.append((high >> 8) & 0xFF)
    return _finalize(payload)


def hsi(hue: float, saturation: float, intensity: float) -> bytes:
    """Hue 0-360 degrees, saturation 0-100, intensity 0-1000."""
    value = max(0, min(MAX_INTENSITY, _round_half_up(intensity)))
    h = max(0, min(360, _round_half_up(hue))) & 0x1FF
    s = max(0, min(100, _round_half_up(saturation))) & 0x7F

    payload = bytearray(10)
    payload[5] = (s & 0x03) << 6
    payload[6] = ((h & 0x07) << 5) | ((s >> 2) & 0x1F)
    payload[7] = ((h >> 3) & 0x3F) | ((value & 0x03) << 6)
    payload[8] = (value >> 2) & 0xFF
    payload[9] = CMD_HSI
    return _finalize(payload)


@dataclass(frozen=True, slots=True)
class EffectState:
    """Decoded first-generation system-effect state."""

    on: bool
    effect: SystemEffect
    intensity: int
    frequency: int
    speed: int = 0
    trigger: int = 0
    kelvin: int | None = None
    variant: int = 0


@dataclass(frozen=True, slots=True)
class BoostState:
    """Decoded Ace Boost-mode state."""

    enabled: bool
    kelvin: int
    gm: int


@dataclass(frozen=True, slots=True)
class FanState:
    """Decoded fan report, retaining undocumented temperature fields raw."""

    mode: FanMode
    fixture_speed: int
    current_temperature_raw: int
    high_temperature_raw: int
    supported_modes: tuple[FanMode, ...]

    @property
    def temperature_c(self) -> int | None:
        """Return the signed temperature used by the app, or ``None`` if absent."""
        if self.current_temperature_raw == 128:
            return None
        if self.current_temperature_raw > 128:
            return self.current_temperature_raw - 256
        return self.current_temperature_raw


@dataclass(frozen=True, slots=True)
class PowerState:
    """Decoded battery and external-power report."""

    source: PowerSource
    battery_percent: int
    runtime_minutes: int
    battery_voltage_raw: int
    external_voltage_raw: int


@dataclass(frozen=True, slots=True)
class VersionState:
    """Decoded controller, driver, protocol, and feature versions."""

    protocol_version: int
    function: int
    led_type: int
    cct_low_raw: int
    cct_high_raw: int
    machine: int
    manual_fx_supported: bool
    program_fx_supported: bool
    picker_fx_supported: bool
    touchbar_fx_supported: bool
    music_fx_supported: bool
    control_hardware_version_raw: int
    control_software_version_raw: int
    driver_hardware_version_raw: int
    driver_software_version_raw: int
    upgrade_type: int
    gatt_version: int

    @property
    def control_hw_version(self) -> str:
        return f"{self.control_hardware_version_raw / 10:.1f}"

    @property
    def control_sw_version(self) -> str:
        return f"{self.control_software_version_raw / 10:.1f}"

    @property
    def driver_hw_version(self) -> str:
        return f"{self.driver_hardware_version_raw / 10:.1f}"

    @property
    def driver_sw_version(self) -> str:
        return f"{self.driver_software_version_raw / 10:.1f}"

    @property
    def cct_min_kelvin(self) -> int:
        return self.cct_low_raw * 100

    @property
    def cct_max_kelvin(self) -> int:
        return self.cct_high_raw * 100


def effect_off() -> bytes:
    """Stop the active first-generation system effect."""
    return _build_payload(
        CMD_SYSTEM_EFFECT,
        (_EFFECT_IDS[SystemEffect.OFF], 64, 8),
    )


def effect(
    system_effect: SystemEffect | str,
    *,
    intensity: float = 180,
    frequency: float = 5,
    speed: float = 5,
    trigger: float | None = None,
    kelvin: float = 5600,
    variant: float = 0,
) -> bytes:
    """Build one Ace 25x first-generation CCT effect command.

    Ace does not advertise HSI or G/M support, so the multi-colour effects are
    always encoded in their CCT mode with neutral G/M. ``variant`` is the
    three-position colour-temperature/type selector used by TV, Fire, and
    Fireworks.
    """
    selected = SystemEffect(system_effect)
    if selected is SystemEffect.OFF:
        return effect_off()

    value = _clamp_round(intensity, 0, MAX_INTENSITY)
    maximum_frequency = (
        10 if selected in {SystemEffect.FIRE, SystemEffect.EXPLOSION} else 11
    )
    rate = _clamp_round(frequency, 1, maximum_frequency)
    effect_speed = _clamp_round(speed, 1, 10)
    effect_trigger = _clamp_round(
        _DEFAULT_EFFECT_TRIGGERS.get(selected, 0) if trigger is None else trigger,
        0,
        3,
    )
    cct_value = _clamp_round(kelvin, ACE_MIN_KELVIN, ACE_MAX_KELVIN) // 10
    effect_variant = _clamp_round(variant, 0, 2)
    effect_id = _EFFECT_IDS[selected]

    # Bit 8 is called sleepMode by the APK. A value of one means active; the
    # Kotlin model stores the inverse as a ``sleep`` boolean.
    common = ((1, 8, 1), (effect_id, 64, 8))

    if selected is SystemEffect.PAPARAZZI:
        return _build_payload(
            CMD_SYSTEM_EFFECT,
            *common,
            (10, 33, 7),  # neutral G/M 100 is divided by ten on the wire
            (cct_value, 40, 10),
            (rate, 50, 4),
            (value, 54, 10),
        )

    if selected is SystemEffect.FIREWORKS:
        return _build_payload(
            CMD_SYSTEM_EFFECT,
            *common,
            (effect_variant, 42, 8),
            (rate, 50, 4),
            (value, 54, 10),
        )

    if selected in {SystemEffect.TV, SystemEffect.FIRE}:
        return _build_payload(
            CMD_SYSTEM_EFFECT,
            *common,
            (effect_variant, 40, 10),
            (rate, 50, 4),
            (value, 54, 10),
        )

    if selected is SystemEffect.LIGHTNING:
        return _build_payload(
            CMD_SYSTEM_EFFECT,
            *common,
            (effect_speed, 27, 4),
            (effect_trigger, 31, 2),
            (10, 33, 7),
            (cct_value, 40, 10),
            (rate, 50, 4),
            (value, 54, 10),
        )

    if selected in {SystemEffect.FAULTY_BULB, SystemEffect.PULSING}:
        return _build_payload(
            CMD_SYSTEM_EFFECT,
            *common,
            (effect_speed, 23, 4),
            (effect_trigger, 27, 2),
            (10, 29, 7),
            (cct_value, 36, 10),
            (value, 46, 10),
            (rate, 56, 4),
            (0, 60, 4),  # effectMode 0 is the Ace-supported CCT path
        )

    # Strobe and Explosion share the same CCT-mode layout. They intentionally
    # have no speed field in this first-generation packet.
    return _build_payload(
        CMD_SYSTEM_EFFECT,
        *common,
        (effect_trigger, 27, 2),
        (10, 29, 7),
        (cct_value, 36, 10),
        (value, 46, 10),
        (rate, 56, 4),
        (0, 60, 4),
    )


def boost(enabled: bool, kelvin: float = 5600, gm: float = 100) -> bytes:
    """Set Ace Boost mode (3800-5500 K in 50 K UI steps)."""
    cct_value = _clamp_round(kelvin, ACE_BOOST_MIN_KELVIN, ACE_BOOST_MAX_KELVIN) // 10
    return _build_payload(
        CMD_BOOST,
        (_clamp_round(gm, 0, 255), 52, 8),
        (cct_value, 60, 11),
        (int(enabled), 71, 1),
    )


def fan_request() -> bytes:
    """Request fan capabilities, mode, RPM, and temperature."""
    return _build_payload(CMD_FAN, write=False)


def fan(mode: FanMode | str, fixture_speed: float = 0) -> bytes:
    """Set a fan mode; manual speed is constrained to the app's 0-1000 RPM UI."""
    selected = FanMode(mode)
    return _build_payload(
        CMD_FAN,
        (_clamp_round(fixture_speed, 0, 1000), 48, 16),
        (_FAN_MODE_IDS[selected], 64, 8),
    )


def power_request() -> bytes:
    """Request power-source, battery, runtime, and voltage data."""
    return _build_payload(CMD_POWER, (1, 8, 24), write=False)


def version_request() -> bytes:
    """Request controller, driver, and protocol versions."""
    return _build_payload(CMD_VERSION, write=False)


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


def decode_effect(payload: bytes) -> EffectState | None:
    """Decode an Ace first-generation system-effect command or report."""
    if not _valid_payload(payload, CMD_SYSTEM_EFFECT):
        return None

    selected = _EFFECTS_BY_ID.get(_get_bits(payload, 64, 8))
    if selected is None:
        return None
    on = bool(_get_bits(payload, 8, 1))
    if selected is SystemEffect.OFF:
        return EffectState(
            on=False,
            effect=selected,
            intensity=0,
            frequency=0,
        )

    speed = 0
    trigger = 0
    kelvin: int | None = None
    variant = 0

    if selected is SystemEffect.PAPARAZZI:
        intensity = _get_bits(payload, 54, 10)
        frequency = _get_bits(payload, 50, 4)
        cct_value = _get_bits(payload, 40, 10)
        if _get_bits(payload, 30, 1):
            cct_value += 1000
        kelvin = cct_value * 10
    elif selected is SystemEffect.FIREWORKS:
        intensity = _get_bits(payload, 54, 10)
        frequency = _get_bits(payload, 50, 4)
        variant = _get_bits(payload, 42, 8)
    elif selected in {SystemEffect.TV, SystemEffect.FIRE}:
        intensity = _get_bits(payload, 54, 10)
        frequency = _get_bits(payload, 50, 4)
        variant = _get_bits(payload, 40, 10)
    elif selected is SystemEffect.LIGHTNING:
        intensity = _get_bits(payload, 54, 10)
        frequency = _get_bits(payload, 50, 4)
        speed = _get_bits(payload, 27, 4)
        trigger = _get_bits(payload, 31, 2)
        cct_value = _get_bits(payload, 40, 10)
        if _get_bits(payload, 24, 1):
            cct_value += 1000
        kelvin = cct_value * 10
    elif selected in {SystemEffect.FAULTY_BULB, SystemEffect.PULSING}:
        if _get_bits(payload, 60, 4) != 0:
            return None
        intensity = _get_bits(payload, 46, 10)
        frequency = _get_bits(payload, 56, 4)
        speed = _get_bits(payload, 23, 4)
        trigger = _get_bits(payload, 27, 2)
        cct_value = _get_bits(payload, 36, 10)
        if _get_bits(payload, 20, 1):
            cct_value += 1000
        kelvin = cct_value * 10
    else:
        if _get_bits(payload, 60, 4) != 0:
            return None
        intensity = _get_bits(payload, 46, 10)
        frequency = _get_bits(payload, 56, 4)
        trigger = _get_bits(payload, 27, 2)
        cct_value = _get_bits(payload, 36, 10)
        if _get_bits(payload, 24, 1):
            cct_value += 1000
        kelvin = cct_value * 10

    return EffectState(
        on=on,
        effect=selected,
        intensity=intensity,
        frequency=frequency,
        speed=speed,
        trigger=trigger,
        kelvin=kelvin,
        variant=variant,
    )


def decode_boost(payload: bytes) -> BoostState | None:
    """Decode an Ace Boost command or report."""
    if not _valid_payload(payload, CMD_BOOST):
        return None
    return BoostState(
        enabled=bool(_get_bits(payload, 71, 1)),
        kelvin=_get_bits(payload, 60, 11) * 10,
        gm=_get_bits(payload, 52, 8),
    )


def decode_fan(payload: bytes) -> FanState | None:
    """Decode a fan response, including its per-mode capability bits."""
    if not _valid_payload(payload, CMD_FAN):
        return None
    mode = _FAN_MODES_BY_ID.get(_get_bits(payload, 64, 8))
    if mode is None:
        return None
    supported = tuple(
        candidate
        for candidate in FanMode
        if _get_bits(payload, _FAN_SUPPORT_BITS[candidate], 1)
    )
    return FanState(
        mode=mode,
        fixture_speed=_get_bits(payload, 48, 16),
        current_temperature_raw=_get_bits(payload, 40, 8),
        high_temperature_raw=_get_bits(payload, 36, 4),
        supported_modes=supported,
    )


def decode_power(payload: bytes, *, protocol_version: int = 0) -> PowerState | None:
    """Decode power data; protocol 42 widened runtime from 9 to 12 bits."""
    if not _valid_payload(payload, CMD_POWER):
        return None
    runtime_start, runtime_width = (21, 12) if protocol_version >= 42 else (24, 9)
    return PowerState(
        source=(
            PowerSource.EXTERNAL if _get_bits(payload, 20, 1) else PowerSource.BATTERY
        ),
        battery_percent=_get_bits(payload, 33, 7),
        runtime_minutes=_get_bits(payload, runtime_start, runtime_width),
        battery_voltage_raw=_get_bits(payload, 40, 16),
        external_voltage_raw=_get_bits(payload, 56, 16),
    )


def decode_version(payload: bytes) -> VersionState | None:
    """Decode the APK's command-zero version/capability report."""
    if not _valid_payload(payload, CMD_VERSION):
        return None

    function = _get_bits(payload, 62, 4)
    cct_low = _get_bits(payload, 50, 7)
    cct_high = _get_bits(payload, 43, 7)
    # Mirror VersionProtocol's compatibility fallback for old CCT fixtures.
    if function == 0 and cct_low != cct_high:
        cct_low = 56
        cct_high = 56

    return VersionState(
        protocol_version=_get_bits(payload, 66, 6),
        function=function,
        led_type=_get_bits(payload, 57, 5),
        cct_low_raw=cct_low,
        cct_high_raw=cct_high,
        machine=_get_bits(payload, 39, 4),
        manual_fx_supported=bool(_get_bits(payload, 38, 1)),
        program_fx_supported=bool(_get_bits(payload, 37, 1)),
        picker_fx_supported=bool(_get_bits(payload, 36, 1)),
        touchbar_fx_supported=bool(_get_bits(payload, 35, 1)),
        music_fx_supported=bool(_get_bits(payload, 34, 1)),
        control_hardware_version_raw=_get_bits(payload, 28, 6),
        control_software_version_raw=_get_bits(payload, 22, 6),
        driver_hardware_version_raw=_get_bits(payload, 16, 6),
        driver_software_version_raw=_get_bits(payload, 10, 6),
        upgrade_type=_get_bits(payload, 9, 1),
        gatt_version=_get_bits(payload, 8, 1),
    )


type ProtocolReport = (
    LightState | EffectState | BoostState | FanState | PowerState | VersionState
)


def decode_report(
    payload: bytes, *, protocol_version: int = 0
) -> ProtocolReport | None:
    """Dispatch any verified Ace state/report page by its seven-bit command."""
    if len(payload) != 10 or payload[0] != sum(payload[1:10]) & 0xFF:
        return None
    command = payload[9] & 0x7F
    if command in {CMD_HSI & 0x7F, CMD_CCT & 0x7F}:
        return decode_status(payload)
    if command == CMD_SYSTEM_EFFECT:
        return decode_effect(payload)
    if command == CMD_BOOST:
        return decode_boost(payload)
    if command == CMD_FAN:
        return decode_fan(payload)
    if command == CMD_POWER:
        return decode_power(payload, protocol_version=protocol_version)
    if command == CMD_VERSION:
        return decode_version(payload)
    return None
