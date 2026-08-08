"""Number platform for optional amaran fixture controls."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import (
    CONF_ADDRESS,
    REVOLUTIONS_PER_MINUTE,
    EntityCategory,
    Platform,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AmaranConfigEntry
from .amaranble import systemfx2, telink
from .const import (
    CONF_SUPPORTS_CCT,
    CONF_SUPPORTS_GM,
    DEFAULT_SUPPORTS_CCT,
    DEFAULT_SUPPORTS_GM,
    DOMAIN,
    MANUFACTURER,
    PROFILE_GENERIC,
)
from .device import AmaranConnectionError, AmaranLight
from .profiles import FixtureProfile, profile_for_entry

PARALLEL_UPDATES = 1


def _system_effect2_supports(profile: FixtureProfile, field: str) -> bool:
    """Return whether any selectable command-34 effect carries one field."""
    return any(
        field in systemfx2.system_effect2_fields(effect)
        for effect in profile.system_effects2
    )


def _gm_unique_id(address: str) -> str:
    return f"{address}_gm"


def _effect_cct_unique_id(address: str) -> str:
    return f"{address}_effect_cct"


def _effect_hue_unique_id(address: str) -> str:
    return f"{address}_effect_hue"


def _effect_saturation_unique_id(address: str) -> str:
    return f"{address}_effect_saturation"


def _effect_gm_unique_id(address: str) -> str:
    return f"{address}_effect_gm"


def _boost_cct_unique_id(address: str) -> str:
    return f"{address}_boost_cct"


def _boost_gm_unique_id(address: str) -> str:
    return f"{address}_boost_gm"


def _fan_manual_speed_unique_id(address: str) -> str:
    return f"{address}_fan_manual_speed"


def _remove_registered_entity(hass: HomeAssistant, unique_id: str) -> None:
    """Remove an entity that no longer belongs to the selected profile."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(Platform.NUMBER, DOMAIN, unique_id)
    if entity_id is not None:
        registry.async_remove(entity_id)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AmaranConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up optional tint and model-specific colour-temperature controls."""
    options = entry.options
    profile = profile_for_entry(entry)
    supports_gm = (
        bool(
            options.get(CONF_SUPPORTS_CCT, DEFAULT_SUPPORTS_CCT)
            and options.get(CONF_SUPPORTS_GM, DEFAULT_SUPPORTS_GM)
        )
        if profile.key == PROFILE_GENERIC
        else profile.supports_gm
    )
    address = entry.data[CONF_ADDRESS]
    entities: list[NumberEntity] = []
    if supports_gm:
        entities.append(AmaranGreenMagentaEntity(entry))
    else:
        _remove_registered_entity(hass, _gm_unique_id(address))

    cct_effects = {
        "Paparazzi",
        "Lightning",
        "Faulty Bulb",
        "Pulsing",
        "Strobe",
        "Explosion",
        "Welding",
    }
    if (
        profile.supports_cct
        and profile.min_kelvin < profile.max_kelvin
        and (
            any(effect in cct_effects for effect in profile.effects)
            or _system_effect2_supports(profile, "kelvin")
        )
    ):
        entities.append(AmaranEffectColorTemperatureEntity(entry))
    else:
        _remove_registered_entity(hass, _effect_cct_unique_id(address))

    hsi_effects = {
        "Faulty Bulb",
        "Pulsing",
        "Strobe",
        "Explosion",
        "Welding",
    }
    if (
        profile.supports_color
        and any(effect in hsi_effects for effect in profile.effects)
    ) or _system_effect2_supports(profile, "hue"):
        entities.append(AmaranEffectHueEntity(entry))
    else:
        _remove_registered_entity(hass, _effect_hue_unique_id(address))

    saturation_effects = {"Color Chase", "Party Lights"}
    if (
        (
            profile.supports_color
            and any(effect in hsi_effects for effect in profile.effects)
        )
        or any(effect in saturation_effects for effect in profile.effects)
        or _system_effect2_supports(profile, "saturation")
    ):
        entities.append(AmaranEffectSaturationEntity(entry))
    else:
        _remove_registered_entity(hass, _effect_saturation_unique_id(address))

    gm_effects = {
        "Paparazzi",
        "Lightning",
        "Faulty Bulb",
        "Pulsing",
        "Strobe",
        "Explosion",
        "Welding",
    }
    if profile.supports_gm and (
        any(effect in gm_effects for effect in profile.effects)
        or _system_effect2_supports(profile, "gm")
    ):
        entities.append(AmaranEffectGreenMagentaEntity(entry))
    else:
        _remove_registered_entity(hass, _effect_gm_unique_id(address))

    if profile.supports_boost:
        entities.append(AmaranBoostColorTemperatureEntity(entry))
    else:
        _remove_registered_entity(hass, _boost_cct_unique_id(address))

    if profile.supports_boost and profile.supports_gm:
        entities.append(AmaranBoostGreenMagentaEntity(entry))
    else:
        _remove_registered_entity(hass, _boost_gm_unique_id(address))

    if telink.FanMode.MANUAL.value in profile.fan_modes:
        entities.append(AmaranFanManualSpeedEntity(entry))
    else:
        _remove_registered_entity(hass, _fan_manual_speed_unique_id(address))

    if entities:
        async_add_entities(entities)


class AmaranGreenMagentaEntity(NumberEntity):
    """Green/magenta tint adjustment used by compatible CCT fixtures."""

    _attr_has_entity_name = True
    _attr_translation_key = "green_magenta"
    _attr_icon = "mdi:invert-colors"
    _attr_should_poll = False
    _attr_native_min_value = -10
    _attr_native_max_value = 10
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER

    def __init__(self, entry: AmaranConfigEntry) -> None:
        self._device: AmaranLight = entry.runtime_data
        address = entry.data[CONF_ADDRESS]
        self._attr_unique_id = _gm_unique_id(address)
        self._attr_native_min_value = self._device.green_magenta_min
        self._attr_native_max_value = self._device.green_magenta_max
        if self._device.profile.catalog_capabilities.steady_color.gm_v2_version:
            self._attr_native_step = 0.1
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

    @property
    def available(self) -> bool:
        return self._device.available

    @property
    def native_value(self) -> float:
        return self._device.preferred_gm

    async def async_set_native_value(self, value: float) -> None:
        try:
            await self._device.async_set_gm(value)
        except AmaranConnectionError as err:
            raise HomeAssistantError(str(err)) from err


class _AmaranColorTemperatureEntity(NumberEntity):
    """Shared Home Assistant wiring for proprietary CCT parameters."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_native_step = 50
    _attr_native_unit_of_measurement = UnitOfTemperature.KELVIN
    _attr_mode = NumberMode.SLIDER

    def __init__(
        self,
        entry: AmaranConfigEntry,
        *,
        unique_id: str,
        minimum: int,
        maximum: int,
    ) -> None:
        self._device: AmaranLight = entry.runtime_data
        address = entry.data[CONF_ADDRESS]
        self._attr_unique_id = unique_id
        self._attr_native_min_value = minimum
        self._attr_native_max_value = maximum
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


class AmaranEffectColorTemperatureEntity(_AmaranColorTemperatureEntity):
    """Colour temperature carried by the active built-in effect."""

    _attr_translation_key = "effect_color_temperature"
    _attr_icon = "mdi:thermometer"

    def __init__(self, entry: AmaranConfigEntry) -> None:
        profile = profile_for_entry(entry)
        super().__init__(
            entry,
            unique_id=_effect_cct_unique_id(entry.data[CONF_ADDRESS]),
            minimum=profile.min_kelvin,
            maximum=profile.max_kelvin,
        )

    @property
    def available(self) -> bool:
        return (
            self._device.connected and self._device.effect_color_temperature_available
        )

    @property
    def native_value(self) -> int | None:
        state = self._device.effect_state
        return None if state is None else getattr(state, "kelvin", None)

    async def async_set_native_value(self, value: float) -> None:
        try:
            await self._device.async_set_effect_kelvin(value)
        except AmaranConnectionError as err:
            raise HomeAssistantError(str(err)) from err


class _AmaranEffectParameterEntity(NumberEntity):
    """Shared wiring for numeric parameters carried by an active effect."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER

    def __init__(self, entry: AmaranConfigEntry, unique_id: str) -> None:
        self._device: AmaranLight = entry.runtime_data
        address = entry.data[CONF_ADDRESS]
        self._attr_unique_id = unique_id
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


class AmaranEffectHueEntity(_AmaranEffectParameterEntity):
    """Hue carried by an active HSI-capable system effect."""

    _attr_translation_key = "effect_hue"
    _attr_icon = "mdi:palette"
    _attr_native_min_value = 0
    _attr_native_max_value = 360

    def __init__(self, entry: AmaranConfigEntry) -> None:
        super().__init__(entry, _effect_hue_unique_id(entry.data[CONF_ADDRESS]))

    @property
    def available(self) -> bool:
        return self._device.connected and self._device.effect_hue_available

    @property
    def native_value(self) -> int | None:
        state = self._device.effect_state
        return None if state is None else getattr(state, "hue", None)

    async def async_set_native_value(self, value: float) -> None:
        try:
            await self._device.async_set_effect_hue(value)
        except AmaranConnectionError as err:
            raise HomeAssistantError(str(err)) from err


class AmaranEffectSaturationEntity(_AmaranEffectParameterEntity):
    """Saturation carried by an active system effect."""

    _attr_translation_key = "effect_saturation"
    _attr_icon = "mdi:water-opacity"
    _attr_native_min_value = 0
    _attr_native_max_value = 100

    def __init__(self, entry: AmaranConfigEntry) -> None:
        super().__init__(entry, _effect_saturation_unique_id(entry.data[CONF_ADDRESS]))

    @property
    def available(self) -> bool:
        return self._device.connected and self._device.effect_saturation_available

    @property
    def native_value(self) -> int | None:
        state = self._device.effect_state
        return None if state is None else getattr(state, "saturation", None)

    async def async_set_native_value(self, value: float) -> None:
        try:
            await self._device.async_set_effect_saturation(value)
        except AmaranConnectionError as err:
            raise HomeAssistantError(str(err)) from err


class AmaranEffectGreenMagentaEntity(_AmaranEffectParameterEntity):
    """Green/magenta tint carried by an active CCT-based system effect."""

    _attr_translation_key = "effect_green_magenta"
    _attr_icon = "mdi:invert-colors"
    _attr_native_min_value = -10
    _attr_native_max_value = 10

    def __init__(self, entry: AmaranConfigEntry) -> None:
        super().__init__(entry, _effect_gm_unique_id(entry.data[CONF_ADDRESS]))
        self._attr_native_min_value = self._device.green_magenta_min
        self._attr_native_max_value = self._device.green_magenta_max
        if self._device.profile.catalog_capabilities.steady_color.gm_v2_version:
            self._attr_native_step = 0.1

    @property
    def available(self) -> bool:
        return self._device.connected and self._device.effect_gm_available

    @property
    def native_value(self) -> float | None:
        state = self._device.effect_state
        gm = None if state is None else getattr(state, "gm", None)
        return None if gm is None else gm / 10 - 10

    async def async_set_native_value(self, value: float) -> None:
        try:
            await self._device.async_set_effect_gm(value)
        except AmaranConnectionError as err:
            raise HomeAssistantError(str(err)) from err


class AmaranBoostColorTemperatureEntity(_AmaranColorTemperatureEntity):
    """Colour temperature used while the Ace Boost preview is active."""

    _attr_translation_key = "boost_color_temperature"
    _attr_icon = "mdi:thermometer-chevron-up"

    def __init__(self, entry: AmaranConfigEntry) -> None:
        profile = profile_for_entry(entry)
        super().__init__(
            entry,
            unique_id=_boost_cct_unique_id(entry.data[CONF_ADDRESS]),
            minimum=profile.boost_min_kelvin or profile.min_kelvin,
            maximum=profile.boost_max_kelvin or profile.max_kelvin,
        )

    @property
    def available(self) -> bool:
        state = self._device.boost_state
        return self._device.connected and state is not None and state.enabled

    @property
    def native_value(self) -> int | None:
        state = self._device.boost_state
        return None if state is None else state.kelvin

    async def async_set_native_value(self, value: float) -> None:
        try:
            await self._device.async_set_boost_kelvin(value)
        except AmaranConnectionError as err:
            raise HomeAssistantError(str(err)) from err


class AmaranBoostGreenMagentaEntity(NumberEntity):
    """Green/magenta tint used by a full-colour fixture's Boost dialog."""

    _attr_has_entity_name = True
    _attr_translation_key = "boost_green_magenta"
    _attr_icon = "mdi:invert-colors"
    _attr_should_poll = False
    _attr_native_min_value = -10
    _attr_native_max_value = 10
    _attr_native_step = 0.1
    _attr_mode = NumberMode.SLIDER

    def __init__(self, entry: AmaranConfigEntry) -> None:
        self._device: AmaranLight = entry.runtime_data
        address = entry.data[CONF_ADDRESS]
        self._attr_unique_id = _boost_gm_unique_id(address)
        self._attr_native_min_value = self._device.green_magenta_min
        self._attr_native_max_value = self._device.green_magenta_max
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

    @property
    def available(self) -> bool:
        state = self._device.boost_state
        return self._device.connected and state is not None and state.enabled

    @property
    def native_value(self) -> float | None:
        state = self._device.boost_state
        return None if state is None else state.gm / 10 - 10

    async def async_set_native_value(self, value: float) -> None:
        try:
            await self._device.async_set_boost_gm(value)
        except AmaranConnectionError as err:
            raise HomeAssistantError(str(err)) from err


class AmaranFanManualSpeedEntity(NumberEntity):
    """Manual fan target reported and confirmed by compatible fixtures."""

    _attr_has_entity_name = True
    _attr_translation_key = "fan_manual_speed"
    _attr_icon = "mdi:fan-speed-1"
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 0
    _attr_native_max_value = 1000
    _attr_native_step = 1
    _attr_native_unit_of_measurement = REVOLUTIONS_PER_MINUTE
    _attr_mode = NumberMode.BOX

    def __init__(self, entry: AmaranConfigEntry) -> None:
        self._device: AmaranLight = entry.runtime_data
        address = entry.data[CONF_ADDRESS]
        self._attr_unique_id = _fan_manual_speed_unique_id(address)
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

    @property
    def available(self) -> bool:
        state = self._device.fan_state
        return (
            self._device.connected
            and state is not None
            and state.mode is telink.FanMode.MANUAL
            and telink.FanMode.MANUAL in state.supported_modes
            and 0 <= state.fixture_speed <= 1000
        )

    @property
    def native_value(self) -> int | None:
        state = self._device.fan_state
        if state is None or not 0 <= state.fixture_speed <= 1000:
            return None
        return state.fixture_speed

    async def async_set_native_value(self, value: float) -> None:
        try:
            await self._device.async_set_fan_speed(value)
        except AmaranConnectionError as err:
            raise HomeAssistantError(str(err)) from err
