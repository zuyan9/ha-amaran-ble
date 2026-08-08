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
CMD_VERSION_2 = 0x25
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
    """First-generation system effects carried by command 7."""

    OFF = "off"
    CLUB_LIGHTS = "Club Lights"
    PAPARAZZI = "Paparazzi"
    FIREWORKS = "Fireworks"
    FAULTY_BULB = "Faulty Bulb"
    LIGHTNING = "Lightning"
    TV = "TV"
    CANDLE = "Candle"
    PULSING = "Pulsing"
    STROBE = "Strobe"
    EXPLOSION = "Explosion"
    FIRE = "Fire"
    WELDING = "Welding"
    COP_CAR = "Cop Car"
    COLOR_CHASE = "Color Chase"
    PARTY_LIGHTS = "Party Lights"


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
    SystemEffect.CLUB_LIGHTS: 0,
    SystemEffect.PAPARAZZI: 1,
    SystemEffect.LIGHTNING: 2,
    SystemEffect.TV: 3,
    SystemEffect.CANDLE: 4,
    SystemEffect.FIRE: 5,
    SystemEffect.STROBE: 6,
    SystemEffect.EXPLOSION: 7,
    SystemEffect.FAULTY_BULB: 8,
    SystemEffect.PULSING: 9,
    SystemEffect.WELDING: 10,
    SystemEffect.COP_CAR: 11,
    SystemEffect.COLOR_CHASE: 12,
    SystemEffect.PARTY_LIGHTS: 13,
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
    SystemEffect.WELDING: 2,
}

_DEFAULT_EFFECT_VARIANTS = {
    # Defaults from the app's Kotlin effect models. Candle and Club use zero.
    SystemEffect.COP_CAR: 2,
}


def _round_half_up(value: float) -> int:
    """Match JavaScript Math.round, including exact half ties toward +infinity."""
    if math.isnan(value):
        return 0
    if math.isinf(value):
        return (2**63 - 1) if value > 0 else -(2**63)
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


def _coarse_gm_value(app_gm: float) -> int:
    """Match the app's asymmetric legacy 0..200 to 0..20 conversion."""
    if math.isnan(app_gm):
        return 0
    value = max(0.0, min(200.0, app_gm))
    # The APK uses Java integer division above neutral, but Math.round at and
    # below neutral. Preserve that quirk for non-integral service inputs too.
    return int(value / 10) if value > 100 else _round_half_up(value / 10)


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


def cct(
    kelvin: float,
    intensity: float,
    gm: float = 0,
    *,
    gm_flag: int | bool = 0,
) -> bytes:
    """Build CCT plus a coarse or G/M-v2 green/magenta shift.

    The normalized G/M domain remains -10..+10. With ``gm_flag`` set, the app
    carries tenths across its exact 0..200 model domain and the adjacent high
    bit distinguishes values above neutral.
    """
    value = max(0, min(MAX_INTENSITY, _round_half_up(intensity)))

    # The app divides positive Kelvin by ten using Java integer division before
    # handing the value to the protocol builder.
    clamped_kelvin = (
        MIN_KELVIN if math.isnan(kelvin) else max(MIN_KELVIN, min(MAX_KELVIN, kelvin))
    )
    tcct = int(clamped_kelvin // 10)

    exact_gm = int(bool(gm_flag))
    if exact_gm:
        app_gm = _clamp_round((gm + 10) * 10, 0, 200)
        gm_high = int(app_gm > 100)
        green = app_gm - (100 if gm_high else 0)
    else:
        gm_high = 0
        green = _coarse_gm_value((gm + 10) * 10)

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
    low |= exact_gm << 43
    low |= gm_high << 44
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
    gm: int | None = None
    gm_flag: bool = False
    variant: int = 0
    mode: int = 0
    hue: int | None = None
    saturation: int | None = None


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
    power_state_raw: bool
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


@dataclass(frozen=True, slots=True)
class Version2State:
    """Decoded command-37 effect, pixel, geometry, and motion capabilities."""

    system_effects_2_supported: bool
    system_effect_groups: tuple[bool, ...]
    pixel_effects_supported: bool
    pixel_effect_groups: tuple[bool, ...]
    pixel_x1: int
    pixel_y1: int
    pixel_x2: int
    pixel_y2: int
    effect_active: bool
    sleeping: bool
    pixel_num: int
    motion_supported: bool

    @property
    def active_system_effect_groups(self) -> tuple[str, ...]:
        """Return the app's enabled command-34 group letters."""
        return tuple(
            chr(ord("A") + index)
            for index, enabled in enumerate(self.system_effect_groups)
            if enabled
        )

    @property
    def active_pixel_effect_groups(self) -> tuple[str, ...]:
        """Return the app's enabled command-33 group letters."""
        return tuple(
            chr(ord("A") + index)
            for index, enabled in enumerate(self.pixel_effect_groups)
            if enabled
        )


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
    speed: float | None = None,
    trigger: float | None = None,
    kelvin: float = 5600,
    gm: float = 100,
    gm_flag: int | bool = 0,
    variant: float | None = None,
    mode: float | None = None,
    hue: float = 0,
    saturation: float | None = None,
) -> bytes:
    """Build one first-generation system-effect command.

    ``gm`` uses the app's raw 0..200 scale, where 100 is neutral. Fixtures with
    the app's G/M-v2 capability set ``gm_flag`` and carry the exact raw value;
    older fixtures quantize it to ten-unit steps. Full-colour Faulty Bulb,
    Pulsing, Strobe, Explosion, and Welding effects default to the app's HSI
    representation while preserving either reported colour mode. ``variant``
    is the APK's preset/colour index; saturation has its own explicit field.
    """
    selected = SystemEffect(system_effect)
    if selected is SystemEffect.OFF:
        return effect_off()

    value = _clamp_round(intensity, 0, MAX_INTENSITY)
    maximum_frequency = (
        10
        if selected
        in {
            SystemEffect.CLUB_LIGHTS,
            SystemEffect.CANDLE,
            SystemEffect.FIRE,
            SystemEffect.EXPLOSION,
            SystemEffect.COLOR_CHASE,
            SystemEffect.PARTY_LIGHTS,
        }
        else 11
    )
    rate = _clamp_round(frequency, 1, maximum_frequency)
    default_speed = 18 if selected is SystemEffect.WELDING else 5
    effect_speed = _clamp_round(
        default_speed if speed is None else speed,
        0 if selected is SystemEffect.WELDING else 1,
        127 if selected is SystemEffect.WELDING else 10,
    )
    effect_trigger = _clamp_round(
        _DEFAULT_EFFECT_TRIGGERS.get(selected, 0) if trigger is None else trigger,
        0,
        3,
    )
    effect_kelvin = _clamp_round(kelvin, MIN_KELVIN, MAX_KELVIN) // 10
    high_cct = effect_kelvin > 1000
    cct_value = effect_kelvin - 1000 if high_cct else effect_kelvin
    effect_gm_flag = int(bool(gm_flag))
    if effect_gm_flag:
        raw_gm = _clamp_round(gm, 0, 200)
        effect_gm_high = int(raw_gm > 100)
        effect_gm = raw_gm - (100 if effect_gm_high else 0)
    else:
        effect_gm_high = 0
        effect_gm = _coarse_gm_value(gm)
    requested_variant = (
        _DEFAULT_EFFECT_VARIANTS.get(selected, 0) if variant is None else variant
    )
    if selected in {
        SystemEffect.TV,
        SystemEffect.CANDLE,
        SystemEffect.FIRE,
        SystemEffect.FIREWORKS,
    }:
        effect_variant = _clamp_round(requested_variant, 0, 2)
    elif selected is SystemEffect.CLUB_LIGHTS:
        effect_variant = _clamp_round(requested_variant, 0, 7)
    elif selected is SystemEffect.COP_CAR:
        effect_variant = _clamp_round(requested_variant, 0, 4)
    else:
        effect_variant = 0
    effect_id = _EFFECT_IDS[selected]

    # Bit 8 is called sleepMode by the APK. A value of one means active; the
    # Kotlin model stores the inverse as a ``sleep`` boolean.
    common = ((1, 8, 1), (effect_id, 64, 8))

    if selected in {SystemEffect.CLUB_LIGHTS, SystemEffect.COP_CAR}:
        return _build_payload(
            CMD_SYSTEM_EFFECT,
            *common,
            (effect_variant, 46, 4),
            (rate, 50, 4),
            (value, 54, 10),
        )

    if selected in {SystemEffect.COLOR_CHASE, SystemEffect.PARTY_LIGHTS}:
        return _build_payload(
            CMD_SYSTEM_EFFECT,
            *common,
            (
                _clamp_round(100 if saturation is None else saturation, 0, 100),
                43,
                7,
            ),
            (rate, 50, 4),
            (value, 54, 10),
        )

    if selected is SystemEffect.CANDLE:
        # Despite the protocol field name, this is the app's three-way
        # ``cct_type`` preset, not a colour temperature in kelvin.
        return _build_payload(
            CMD_SYSTEM_EFFECT,
            *common,
            (effect_variant, 40, 10),
            (rate, 50, 4),
            (value, 54, 10),
        )

    if selected is SystemEffect.PAPARAZZI:
        return _build_payload(
            CMD_SYSTEM_EFFECT,
            *common,
            (effect_gm_flag, 31, 1),
            (effect_gm_high, 32, 1),
            (effect_gm, 33, 7),
            (int(high_cct), 30, 1),
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
            (effect_gm_flag, 25, 1),
            (effect_gm_high, 26, 1),
            (effect_speed, 27, 4),
            (effect_trigger, 31, 2),
            (effect_gm, 33, 7),
            (int(high_cct), 24, 1),
            (cct_value, 40, 10),
            (rate, 50, 4),
            (value, 54, 10),
        )

    if selected is SystemEffect.WELDING:
        # The app defaults Welding to HSI mode. It can also report a CCT mode;
        # preserve either representation so subsequent parameter updates do
        # not silently change the effect's colour mode.
        effect_mode = _clamp_round(1 if mode is None else mode, 0, 1)
        if effect_mode == 1:
            return _build_payload(
                CMD_SYSTEM_EFFECT,
                *common,
                (effect_speed, 21, 7),  # APK field name: min
                (effect_trigger, 28, 2),
                (
                    _clamp_round(100 if saturation is None else saturation, 0, 100),
                    30,
                    7,
                ),
                (_clamp_round(hue, 0, 360), 37, 9),
                (value, 46, 10),
                (rate, 56, 4),
                (effect_mode, 60, 4),
            )
        return _build_payload(
            CMD_SYSTEM_EFFECT,
            *common,
            (int(high_cct), 17, 1),
            (effect_gm_flag, 18, 1),
            (effect_gm_high, 19, 1),
            (effect_speed, 20, 7),
            (effect_trigger, 27, 2),
            (effect_gm, 29, 7),
            (cct_value, 36, 10),
            (value, 46, 10),
            (rate, 56, 4),
            (effect_mode, 60, 4),
        )

    if selected in {SystemEffect.FAULTY_BULB, SystemEffect.PULSING}:
        effect_mode = _clamp_round(1 if mode is None else mode, 0, 1)
        if effect_mode == 1:
            return _build_payload(
                CMD_SYSTEM_EFFECT,
                *common,
                (effect_speed, 24, 4),
                (effect_trigger, 28, 2),
                (
                    _clamp_round(100 if saturation is None else saturation, 0, 100),
                    30,
                    7,
                ),
                (_clamp_round(hue, 0, 360), 37, 9),
                (value, 46, 10),
                (rate, 56, 4),
                (effect_mode, 60, 4),
            )
        return _build_payload(
            CMD_SYSTEM_EFFECT,
            *common,
            (effect_gm_flag, 21, 1),
            (effect_gm_high, 22, 1),
            (effect_speed, 23, 4),
            (effect_trigger, 27, 2),
            (effect_gm, 29, 7),
            (int(high_cct), 20, 1),
            (cct_value, 36, 10),
            (value, 46, 10),
            (rate, 56, 4),
            (0, 60, 4),  # effectMode 0 is the Ace-supported CCT path
        )

    # Strobe and Explosion share layouts and intentionally have no speed field.
    effect_mode = _clamp_round(1 if mode is None else mode, 0, 1)
    if effect_mode == 1:
        return _build_payload(
            CMD_SYSTEM_EFFECT,
            *common,
            (effect_trigger, 28, 2),
            (
                _clamp_round(100 if saturation is None else saturation, 0, 100),
                30,
                7,
            ),
            (_clamp_round(hue, 0, 360), 37, 9),
            (value, 46, 10),
            (rate, 56, 4),
            (effect_mode, 60, 4),
        )
    return _build_payload(
        CMD_SYSTEM_EFFECT,
        *common,
        (effect_gm_flag, 25, 1),
        (effect_gm_high, 26, 1),
        (effect_trigger, 27, 2),
        (effect_gm, 29, 7),
        (int(high_cct), 24, 1),
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


def version2_request() -> bytes:
    """Request advanced system-effect, pixel, geometry, and motion support."""
    return _build_payload(CMD_VERSION_2, write=False)


@dataclass(frozen=True)
class LightState:
    """A decoded status report from a fixture."""

    on: bool
    is_hsi: bool
    intensity: int  # 0-1000
    kelvin: int  # CCT mode only
    gm: float  # -10..+10, CCT mode only; tenths when G/M-v2 is set
    hue: int  # 0-360, HSI mode only
    saturation: int  # 0-100, HSI mode only
    gm_flag: bool = False


def decode_status(payload: bytes) -> LightState | None:
    """Decode a 10-byte status payload; ``None`` if it is not a state report.

    Fixtures also emit a ``0x0a`` diagnostic page that carries no light state.
    """
    if len(payload) != 10 or payload[0] != sum(payload[1:10]) & 0xFF:
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
        gm_flag = bool((low >> 43) & 0x01)
        gm_value = (low >> 45) & 0x7F
        gm = (
            max(
                -10.0,
                min(
                    10.0,
                    (((low >> 44) & 0x01) * 100 + gm_value - 100) / 10,
                ),
            )
            if gm_flag
            else max(-10, min(10, gm_value - 10))
        )
        return LightState(
            on=on,
            is_hsi=False,
            intensity=intensity,
            kelvin=telink_cct * 10,
            gm=gm,
            hue=0,
            saturation=0,
            gm_flag=gm_flag,
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


def _decode_effect_gm(
    payload: bytes,
    gm_flag: bool,
    high_bit: int,
    value_bit: int,
) -> int:
    """Decode the legacy coarse or G/M-v2 exact raw effect value."""
    value = _get_bits(payload, value_bit, 7)
    if not gm_flag:
        return value * 10
    return value + 100 * _get_bits(payload, high_bit, 1)


def decode_effect(payload: bytes) -> EffectState | None:
    """Decode a first-generation system-effect command or report."""
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
    gm: int | None = None
    gm_flag = False
    variant = 0
    mode = 0
    hue: int | None = None
    saturation: int | None = None

    if selected in {SystemEffect.CLUB_LIGHTS, SystemEffect.COP_CAR}:
        intensity = _get_bits(payload, 54, 10)
        frequency = _get_bits(payload, 50, 4)
        variant = _get_bits(payload, 46, 4)
    elif selected in {SystemEffect.COLOR_CHASE, SystemEffect.PARTY_LIGHTS}:
        intensity = _get_bits(payload, 54, 10)
        frequency = _get_bits(payload, 50, 4)
        saturation = _get_bits(payload, 43, 7)
    elif selected is SystemEffect.CANDLE:
        intensity = _get_bits(payload, 54, 10)
        frequency = _get_bits(payload, 50, 4)
        variant = _get_bits(payload, 40, 10)
    elif selected is SystemEffect.PAPARAZZI:
        intensity = _get_bits(payload, 54, 10)
        frequency = _get_bits(payload, 50, 4)
        cct_value = _get_bits(payload, 40, 10)
        if _get_bits(payload, 30, 1):
            cct_value += 1000
        kelvin = cct_value * 10
        gm_flag = bool(_get_bits(payload, 31, 1))
        gm = _decode_effect_gm(payload, gm_flag, 32, 33)
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
        gm_flag = bool(_get_bits(payload, 25, 1))
        gm = _decode_effect_gm(payload, gm_flag, 26, 33)
    elif selected is SystemEffect.WELDING:
        mode = _get_bits(payload, 60, 4)
        if mode not in {0, 1}:
            return None
        intensity = _get_bits(payload, 46, 10)
        frequency = _get_bits(payload, 56, 4)
        if mode == 0:
            speed = _get_bits(payload, 20, 7)
            trigger = _get_bits(payload, 27, 2)
            cct_value = _get_bits(payload, 36, 10)
            if _get_bits(payload, 17, 1):
                cct_value += 1000
            kelvin = cct_value * 10
            gm_flag = bool(_get_bits(payload, 18, 1))
            gm = _decode_effect_gm(payload, gm_flag, 19, 29)
        else:
            speed = _get_bits(payload, 21, 7)
            trigger = _get_bits(payload, 28, 2)
            saturation = _get_bits(payload, 30, 7)
            hue = _get_bits(payload, 37, 9)
    elif selected in {SystemEffect.FAULTY_BULB, SystemEffect.PULSING}:
        mode = _get_bits(payload, 60, 4)
        if mode not in {0, 1}:
            return None
        intensity = _get_bits(payload, 46, 10)
        frequency = _get_bits(payload, 56, 4)
        if mode == 1:
            speed = _get_bits(payload, 24, 4)
            trigger = _get_bits(payload, 28, 2)
            saturation = _get_bits(payload, 30, 7)
            hue = _get_bits(payload, 37, 9)
        else:
            speed = _get_bits(payload, 23, 4)
            trigger = _get_bits(payload, 27, 2)
            cct_value = _get_bits(payload, 36, 10)
            if _get_bits(payload, 20, 1):
                cct_value += 1000
            kelvin = cct_value * 10
            gm_flag = bool(_get_bits(payload, 21, 1))
            gm = _decode_effect_gm(payload, gm_flag, 22, 29)
    else:
        mode = _get_bits(payload, 60, 4)
        if mode not in {0, 1}:
            return None
        intensity = _get_bits(payload, 46, 10)
        frequency = _get_bits(payload, 56, 4)
        if mode == 1:
            trigger = _get_bits(payload, 28, 2)
            saturation = _get_bits(payload, 30, 7)
            hue = _get_bits(payload, 37, 9)
        else:
            trigger = _get_bits(payload, 27, 2)
            cct_value = _get_bits(payload, 36, 10)
            if _get_bits(payload, 24, 1):
                cct_value += 1000
            kelvin = cct_value * 10
            gm_flag = bool(_get_bits(payload, 25, 1))
            gm = _decode_effect_gm(payload, gm_flag, 26, 29)

    return EffectState(
        on=on,
        effect=selected,
        intensity=intensity,
        frequency=frequency,
        speed=speed,
        trigger=trigger,
        kelvin=kelvin,
        gm=gm,
        gm_flag=gm_flag,
        variant=variant,
        mode=mode,
        hue=hue,
        saturation=saturation,
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
    external_voltage_raw = _get_bits(payload, 56, 16)
    return PowerState(
        # Sidus Link renders "AC Power" whenever this voltage is non-zero.
        # Bit 20 is a separate power/standby state and must not select the source.
        source=PowerSource.EXTERNAL if external_voltage_raw else PowerSource.BATTERY,
        power_state_raw=bool(_get_bits(payload, 20, 1)),
        battery_percent=_get_bits(payload, 33, 7),
        runtime_minutes=_get_bits(payload, runtime_start, runtime_width),
        battery_voltage_raw=_get_bits(payload, 40, 16),
        external_voltage_raw=external_voltage_raw,
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


def decode_version2(payload: bytes) -> Version2State | None:
    """Decode the app's command-37 advanced-capability report."""
    if not _valid_payload(payload, CMD_VERSION_2):
        return None
    return Version2State(
        system_effects_2_supported=bool(_get_bits(payload, 71, 1)),
        system_effect_groups=tuple(
            bool(_get_bits(payload, bit, 1)) for bit in range(70, 62, -1)
        ),
        pixel_effects_supported=bool(_get_bits(payload, 62, 1)),
        pixel_effect_groups=tuple(
            bool(_get_bits(payload, bit, 1)) for bit in range(61, 54, -1)
        ),
        pixel_x1=_get_bits(payload, 51, 4),
        pixel_y1=_get_bits(payload, 47, 4),
        pixel_x2=_get_bits(payload, 41, 6),
        pixel_y2=_get_bits(payload, 35, 6),
        effect_active=bool(_get_bits(payload, 34, 1)),
        sleeping=bool(_get_bits(payload, 33, 1)),
        pixel_num=_get_bits(payload, 29, 4),
        motion_supported=bool(_get_bits(payload, 28, 1)),
    )


type ProtocolReport = (
    LightState
    | EffectState
    | BoostState
    | FanState
    | PowerState
    | VersionState
    | Version2State
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
    if command == CMD_VERSION_2:
        return decode_version2(payload)
    return None
