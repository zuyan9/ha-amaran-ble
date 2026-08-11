"""Light platform for amaran BLE fixtures."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_EFFECT,
    ATTR_HS_COLOR,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AmaranConfigEntry
from .brightness import (
    MAX_INTENSITY,
    brightness_to_intensity,
    intensity_to_brightness,
)
from .const import (
    CONF_MAX_KELVIN,
    CONF_MIN_KELVIN,
    CONF_SUPPORTS_CCT,
    CONF_SUPPORTS_COLOR,
    DEFAULT_MAX_KELVIN,
    DEFAULT_MIN_KELVIN,
    DEFAULT_SUPPORTS_CCT,
    DEFAULT_SUPPORTS_COLOR,
    DOMAIN,
    MANUFACTURER,
    PROFILE_GENERIC,
)
from .device import AmaranConnectionError, AmaranLight
from .profiles import EFFECT_OFF

_LOGGER = logging.getLogger(__name__)

# Let rapid slider calls reach the device-level latest-wins arbiter while the
# previous value is still awaiting confirmation. AmaranLight._operation_lock
# remains the sole owner of packet serialization, so BLE transactions cannot
# interleave even though Home Assistant may enter entity actions concurrently.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AmaranConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([AmaranLightEntity(entry)])


class AmaranLightEntity(LightEntity):
    """A single amaran fixture."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False

    def __init__(self, entry: AmaranConfigEntry) -> None:
        self._device: AmaranLight = entry.runtime_data
        address = entry.data[CONF_ADDRESS]

        self._attr_unique_id = address
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            connections={(CONNECTION_BLUETOOTH, address)},
            manufacturer=self._device.profile.manufacturer or MANUFACTURER,
            model=self._device.profile.name,
            name=entry.title,
        )

        options = entry.options
        profile = self._device.profile
        if profile.key == PROFILE_GENERIC:
            self._supports_cct = bool(
                options.get(CONF_SUPPORTS_CCT, DEFAULT_SUPPORTS_CCT)
            )
            supports_color = bool(
                options.get(CONF_SUPPORTS_COLOR, DEFAULT_SUPPORTS_COLOR)
            )
            self._min_kelvin = int(options.get(CONF_MIN_KELVIN, DEFAULT_MIN_KELVIN))
            self._max_kelvin = int(options.get(CONF_MAX_KELVIN, DEFAULT_MAX_KELVIN))
        else:
            self._supports_cct = profile.supports_cct
            supports_color = profile.supports_color
            self._min_kelvin = profile.min_kelvin
            self._max_kelvin = profile.max_kelvin

        supported_color_modes: set[ColorMode] = set()
        if self._supports_cct:
            supported_color_modes.add(ColorMode.COLOR_TEMP)
        if supports_color:
            supported_color_modes.add(ColorMode.HS)
        if not supported_color_modes:
            supported_color_modes.add(ColorMode.BRIGHTNESS)
        self._attr_supported_color_modes = supported_color_modes
        if profile.supports_effects:
            self._attr_supported_features = LightEntityFeature.EFFECT
            self._attr_effect_list = list(profile.all_effects)

        if self._supports_cct:
            self._attr_min_color_temp_kelvin = self._min_kelvin
            self._attr_max_color_temp_kelvin = self._max_kelvin

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._device.add_listener(self._handle_update))

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self._device.available

    @property
    def is_on(self) -> bool | None:
        effect = self._device.effect_state
        if effect is not None:
            return effect.on
        state = self._device.state
        return None if state is None else state.on

    @property
    def brightness(self) -> int | None:
        effect = self._device.effect_state
        if effect is not None and effect.intensity is not None:
            return intensity_to_brightness(effect.intensity)
        state = self._device.state
        return None if state is None else intensity_to_brightness(state.intensity)

    @property
    def effect(self) -> str | None:
        if not self._device.profile.supports_effects:
            return None
        state = self._device.effect_state
        return EFFECT_OFF if state is None else state.effect.value

    @property
    def color_mode(self) -> ColorMode | None:
        if self._device.effect_state is not None:
            return ColorMode.BRIGHTNESS
        state = self._device.state
        if state is None:
            return None
        if state.is_hsi and ColorMode.HS in self._attr_supported_color_modes:
            return ColorMode.HS
        if ColorMode.COLOR_TEMP in self._attr_supported_color_modes:
            return ColorMode.COLOR_TEMP
        if ColorMode.BRIGHTNESS in self._attr_supported_color_modes:
            return ColorMode.BRIGHTNESS
        # An HSI-only profile can boot in the fixture's default CCT mode. Keep
        # the reported mode inside Home Assistant's advertised mode set.
        return ColorMode.HS

    @property
    def color_temp_kelvin(self) -> int | None:
        if self._device.effect_state is not None:
            return None
        state = self._device.state
        if state is None or state.is_hsi or not self._supports_cct:
            return None
        return min(max(state.kelvin, self._min_kelvin), self._max_kelvin)

    @property
    def hs_color(self) -> tuple[float, float] | None:
        if self._device.effect_state is not None:
            return None
        state = self._device.state
        if (
            state is None
            or not state.is_hsi
            or ColorMode.HS not in self._attr_supported_color_modes
        ):
            return None
        return (float(state.hue), float(state.saturation))

    async def async_turn_on(self, **kwargs: Any) -> None:
        state = self._device.state
        effect_state = self._device.effect_state
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        requested_effect = kwargs.get(ATTR_EFFECT)
        color_temp = kwargs.get(ATTR_COLOR_TEMP_KELVIN)
        hs_color = (
            kwargs.get(ATTR_HS_COLOR)
            if ColorMode.HS in self._attr_supported_color_modes
            else None
        )
        if not self._supports_cct:
            color_temp = None

        if brightness is not None:
            intensity = brightness_to_intensity(brightness)
        elif (
            effect_state is not None
            and effect_state.intensity is not None
            and effect_state.intensity > 0
        ):
            intensity = effect_state.intensity
        elif state is not None and state.intensity > 0:
            intensity = state.intensity
        else:
            intensity = MAX_INTENSITY

        try:
            if requested_effect is not None:
                await self._device.async_apply_effect(
                    requested_effect,
                    intensity=intensity if brightness is not None else None,
                )
                return
            if color_temp is not None:
                color_temp = min(
                    max(color_temp, self._min_kelvin),
                    self._max_kelvin,
                )
            await self._device.async_apply_turn_on(
                intensity=intensity,
                brightness_changed=brightness is not None,
                kelvin=color_temp,
                hs_color=hs_color,
            )
        except AmaranConnectionError as err:
            raise HomeAssistantError(str(err)) from err

    async def async_turn_off(self, **kwargs: Any) -> None:
        try:
            await self._device.async_apply_turn_off()
        except AmaranConnectionError as err:
            raise HomeAssistantError(str(err)) from err
