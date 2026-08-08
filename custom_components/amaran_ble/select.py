"""Select platform for amaran BLE fixtures."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import CONF_ADDRESS, EntityCategory, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AmaranConfigEntry
from .amaranble.telink import SystemEffect
from .const import DOMAIN, MANUFACTURER
from .device import AmaranConnectionError, AmaranLight
from .profiles import profile_for_entry

PARALLEL_UPDATES = 1


def _fan_mode_unique_id(address: str) -> str:
    return f"{address}_fan_mode"


def _effect_rate_unique_id(address: str) -> str:
    return f"{address}_effect_rate"


def _effect_color_unique_id(address: str) -> str:
    return f"{address}_effect_color"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AmaranConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up verified fan and effect choices for the fixture profile."""
    profile = profile_for_entry(entry)
    address = entry.data[CONF_ADDRESS]
    entities: list[SelectEntity] = []
    if profile.supports_fan:
        entities.append(AmaranFanModeEntity(entry))
    else:
        registry = er.async_get(hass)
        entity_id = registry.async_get_entity_id(
            Platform.SELECT, DOMAIN, _fan_mode_unique_id(address)
        )
        if entity_id is not None:
            registry.async_remove(entity_id)

    if profile.supports_effects:
        entities.extend([AmaranEffectRateEntity(entry), AmaranEffectColorEntity(entry)])
    else:
        registry = er.async_get(hass)
        for unique_id in (
            _effect_rate_unique_id(address),
            _effect_color_unique_id(address),
        ):
            entity_id = registry.async_get_entity_id(Platform.SELECT, DOMAIN, unique_id)
            if entity_id is not None:
                registry.async_remove(entity_id)

    if entities:
        async_add_entities(entities)


class AmaranFanModeEntity(SelectEntity):
    """Choose the fixture's supported cooling strategy."""

    _attr_has_entity_name = True
    _attr_translation_key = "fan_mode"
    _attr_icon = "mdi:fan"
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: AmaranConfigEntry) -> None:
        self._device: AmaranLight = entry.runtime_data
        address = entry.data[CONF_ADDRESS]
        self._attr_unique_id = _fan_mode_unique_id(address)
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
        return self._device.connected and bool(self._device.available_fan_modes)

    @property
    def options(self) -> list[str]:
        """Expose only modes the latest fixture report says it supports."""
        return list(self._device.available_fan_modes)

    @property
    def current_option(self) -> str | None:
        state = self._device.fan_state
        if state is None:
            return None
        mode = state.mode
        option = str(getattr(mode, "value", mode))
        return option if option in self.options else None

    async def async_select_option(self, option: str) -> None:
        try:
            await self._device.async_set_fan_mode(option)
        except AmaranConnectionError as err:
            raise HomeAssistantError(str(err)) from err


class AmaranEffectRateEntity(SelectEntity):
    """Frequency/rate of the active built-in effect."""

    _attr_has_entity_name = True
    _attr_translation_key = "effect_rate"
    _attr_icon = "mdi:speedometer"
    _attr_should_poll = False

    def __init__(self, entry: AmaranConfigEntry) -> None:
        self._device: AmaranLight = entry.runtime_data
        address = entry.data[CONF_ADDRESS]
        self._attr_unique_id = _effect_rate_unique_id(address)
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
        return self._device.available and self._device.effect_frequency_available

    @property
    def options(self) -> list[str]:
        state = self._device.effect_state
        options = [str(value) for value in range(1, 11)]
        if state is None or state.effect not in {
            SystemEffect.FIRE,
            SystemEffect.EXPLOSION,
        }:
            options.append("random")
        return options

    @property
    def current_option(self) -> str | None:
        state = self._device.effect_state
        if state is None:
            return None
        option = "random" if state.frequency == 11 else str(state.frequency)
        return option if option in self.options else None

    async def async_select_option(self, option: str) -> None:
        value = 11 if option == "random" else int(option)
        try:
            await self._device.async_set_effect_frequency(value)
        except AmaranConnectionError as err:
            raise HomeAssistantError(str(err)) from err


class AmaranEffectColorEntity(SelectEntity):
    """App-defined white presets for TV, Fire, and Fireworks."""

    _attr_has_entity_name = True
    _attr_translation_key = "effect_color"
    _attr_icon = "mdi:palette"
    _attr_should_poll = False

    def __init__(self, entry: AmaranConfigEntry) -> None:
        self._device: AmaranLight = entry.runtime_data
        address = entry.data[CONF_ADDRESS]
        self._attr_unique_id = _effect_color_unique_id(address)
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
        return self._device.connected and bool(self._device.effect_variant_options)

    @property
    def options(self) -> list[str]:
        return list(self._device.effect_variant_options)

    @property
    def current_option(self) -> str | None:
        state = self._device.effect_state
        options = self.options
        if state is None or not 0 <= state.variant < len(options):
            return None
        return options[state.variant]

    async def async_select_option(self, option: str) -> None:
        try:
            await self._device.async_set_effect_variant(option)
        except AmaranConnectionError as err:
            raise HomeAssistantError(str(err)) from err
