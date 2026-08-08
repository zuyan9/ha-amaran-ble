"""Sensor platform for amaran BLE fixtures."""

from __future__ import annotations

from typing import Any, ClassVar

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    CONF_ADDRESS,
    PERCENTAGE,
    REVOLUTIONS_PER_MINUTE,
    EntityCategory,
    Platform,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AmaranConfigEntry
from .const import DOMAIN, MANUFACTURER
from .device import AmaranLight
from .profiles import profile_for_entry

PARALLEL_UPDATES = 1

_SENSOR_SUFFIXES = (
    "battery",
    "remaining_runtime",
    "power_source",
    "protocol_version",
    "fan_speed",
    "fan_temperature",
)


def _sensor_unique_id(address: str, suffix: str) -> str:
    return f"{address}_{suffix}"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AmaranConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up runtime-discovered power, version, and fan diagnostics."""
    profile = profile_for_entry(entry)
    cataloged = bool(profile.app_product_ids)
    entities: list[SensorEntity] = []
    wanted_suffixes: set[str] = set()
    if (
        profile.supports_power
        or profile.supports_version
        or profile.supports_fan
        or cataloged
    ):
        if profile.supports_power or cataloged:
            entities.extend(
                [
                    AmaranBatteryEntity(entry),
                    AmaranRemainingRuntimeEntity(entry),
                    AmaranPowerSourceEntity(entry),
                ]
            )
            wanted_suffixes.update({"battery", "remaining_runtime", "power_source"})
        if profile.supports_version or cataloged:
            entities.append(AmaranProtocolVersionEntity(entry))
            wanted_suffixes.add("protocol_version")
        if profile.supports_fan:
            entities.extend(
                [AmaranFanSpeedEntity(entry), AmaranFanTemperatureEntity(entry)]
            )
            wanted_suffixes.update({"fan_speed", "fan_temperature"})

    registry = er.async_get(hass)
    address = entry.data[CONF_ADDRESS]
    for suffix in _SENSOR_SUFFIXES:
        if suffix in wanted_suffixes:
            continue
        entity_id = registry.async_get_entity_id(
            Platform.SENSOR, DOMAIN, _sensor_unique_id(address, suffix)
        )
        if entity_id is not None:
            registry.async_remove(entity_id)

    if entities:
        async_add_entities(entities)


class _AmaranDiagnosticSensor(SensorEntity):
    """Shared listener and device metadata for diagnostics."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: AmaranConfigEntry, suffix: str) -> None:
        self._device: AmaranLight = entry.runtime_data
        address = entry.data[CONF_ADDRESS]
        self._attr_unique_id = _sensor_unique_id(address, suffix)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            connections={(CONNECTION_BLUETOOTH, address)},
            manufacturer=self._device.profile.manufacturer or MANUFACTURER,
            name=entry.title,
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._device.add_listener(self._handle_update))

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()


class _AmaranPowerSensor(_AmaranDiagnosticSensor):
    """Base for values carried by the fixture power status page."""

    @property
    def available(self) -> bool:
        return self._device.connected and self._device.power_state is not None


class AmaranBatteryEntity(_AmaranPowerSensor):
    """Remaining internal battery percentage."""

    _attr_translation_key = "battery"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, entry: AmaranConfigEntry) -> None:
        super().__init__(entry, "battery")

    @property
    def native_value(self) -> int | None:
        state = self._device.power_state
        return None if state is None else state.battery_percent


class AmaranRemainingRuntimeEntity(_AmaranPowerSensor):
    """Fixture-reported estimated battery runtime."""

    _attr_translation_key = "remaining_runtime"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, entry: AmaranConfigEntry) -> None:
        super().__init__(entry, "remaining_runtime")

    @property
    def native_value(self) -> int | None:
        state = self._device.power_state
        return None if state is None else state.runtime_minutes


class AmaranPowerSourceEntity(_AmaranPowerSensor):
    """Whether the fixture currently runs from its battery or external input."""

    _attr_translation_key = "power_source"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options: ClassVar[list[str]] = ["battery", "external"]

    def __init__(self, entry: AmaranConfigEntry) -> None:
        super().__init__(entry, "power_source")

    @property
    def native_value(self) -> str | None:
        state = self._device.power_state
        if state is None:
            return None
        source = state.source
        return str(getattr(source, "value", source))


class AmaranProtocolVersionEntity(_AmaranDiagnosticSensor):
    """Protocol version plus the controller/driver version tuple."""

    _attr_translation_key = "protocol_version"
    _attr_icon = "mdi:chip"

    def __init__(self, entry: AmaranConfigEntry) -> None:
        super().__init__(entry, "protocol_version")

    @property
    def available(self) -> bool:
        return self._device.connected and self._device.version_state is not None

    @property
    def native_value(self) -> str | None:
        state = self._device.version_state
        return None if state is None else str(state.protocol_version)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        state = self._device.version_state
        if state is None:
            return None
        attributes: dict[str, Any] = {
            "control_hardware_version": state.control_hw_version,
            "control_software_version": state.control_sw_version,
            "driver_hardware_version": state.driver_hw_version,
            "driver_software_version": state.driver_sw_version,
            "minimum_color_temperature": state.cct_min_kelvin,
            "maximum_color_temperature": state.cct_max_kelvin,
            "manual_effects_supported": state.manual_fx_supported,
            "program_effects_supported": state.program_fx_supported,
            "picker_effects_supported": state.picker_fx_supported,
            "touchbar_effects_supported": state.touchbar_fx_supported,
            "music_effects_supported": state.music_fx_supported,
        }
        advanced = self._device.version2_state
        if advanced is not None:
            attributes.update(
                {
                    "system_effects_2_supported": advanced.system_effects_2_supported,
                    "system_effect_groups": list(advanced.active_system_effect_groups),
                    "pixel_effects_supported": advanced.pixel_effects_supported,
                    "pixel_effect_groups": list(advanced.active_pixel_effect_groups),
                    "pixel_geometry": [
                        [advanced.pixel_x1, advanced.pixel_y1],
                        [advanced.pixel_x2, advanced.pixel_y2],
                    ],
                    "pixel_num": advanced.pixel_num,
                    "motion_supported": advanced.motion_supported,
                }
            )
        return attributes


class _AmaranFanSensor(_AmaranDiagnosticSensor):
    """Base for values carried by the fan status page."""

    @property
    def available(self) -> bool:
        return self._device.connected and self._device.fan_state is not None


class AmaranFanSpeedEntity(_AmaranFanSensor):
    """Fixture-reported current fan speed."""

    _attr_translation_key = "fan_speed"
    _attr_icon = "mdi:fan"
    _attr_native_unit_of_measurement = REVOLUTIONS_PER_MINUTE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, entry: AmaranConfigEntry) -> None:
        super().__init__(entry, "fan_speed")

    @property
    def native_value(self) -> int | None:
        state = self._device.fan_state
        if state is None or state.fixture_speed == 0xFFFF:
            return None
        return state.fixture_speed


class AmaranFanTemperatureEntity(_AmaranFanSensor):
    """Fixture-reported internal temperature and warning flag."""

    _attr_translation_key = "fan_temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, entry: AmaranConfigEntry) -> None:
        super().__init__(entry, "fan_temperature")

    @property
    def native_value(self) -> int | None:
        state = self._device.fan_state
        return None if state is None else state.temperature_c

    @property
    def extra_state_attributes(self) -> dict[str, bool] | None:
        state = self._device.fan_state
        if state is None:
            return None
        return {"high_temperature": bool(state.high_temperature_raw)}
