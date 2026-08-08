"""Number platform for optional amaran fixture controls."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import CONF_ADDRESS, Platform, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AmaranConfigEntry
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
from .profiles import profile_for_entry

PARALLEL_UPDATES = 1


def _gm_unique_id(address: str) -> str:
    return f"{address}_gm"


def _effect_cct_unique_id(address: str) -> str:
    return f"{address}_effect_cct"


def _boost_cct_unique_id(address: str) -> str:
    return f"{address}_boost_cct"


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

    if profile.supports_effects:
        entities.append(AmaranEffectColorTemperatureEntity(entry))
    else:
        _remove_registered_entity(hass, _effect_cct_unique_id(address))

    if profile.supports_boost:
        entities.append(AmaranBoostColorTemperatureEntity(entry))
    else:
        _remove_registered_entity(hass, _boost_cct_unique_id(address))

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
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            connections={(CONNECTION_BLUETOOTH, address)},
            manufacturer=MANUFACTURER,
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
    def native_value(self) -> int:
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
            manufacturer=MANUFACTURER,
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
        return None if state is None else state.kelvin

    async def async_set_native_value(self, value: float) -> None:
        try:
            await self._device.async_set_effect_kelvin(value)
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
