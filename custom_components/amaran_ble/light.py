"""Light platform for amaran BLE fixtures."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_HS_COLOR,
    ColorMode,
    LightEntity,
)
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AmaranConfigEntry
from .const import (
    CONF_MAX_KELVIN,
    CONF_MIN_KELVIN,
    CONF_SUPPORTS_COLOR,
    DEFAULT_MAX_KELVIN,
    DEFAULT_MIN_KELVIN,
    DEFAULT_SUPPORTS_COLOR,
    DOMAIN,
    MANUFACTURER,
)
from .device import AmaranConnectionError, AmaranLight

_LOGGER = logging.getLogger(__name__)

# A turn-on can be a three-message transaction (parameters, power, refresh).
# Keep entity service calls from interleaving those messages.
PARALLEL_UPDATES = 1

# The fixtures work in tenths of a percent; Home Assistant works in 0-255.
MAX_INTENSITY = 1000


def _to_intensity(brightness: int) -> int:
    return max(1, round(brightness / 255 * MAX_INTENSITY))


def _to_brightness(intensity: int) -> int:
    return max(1, round(intensity / MAX_INTENSITY * 255))


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
            manufacturer=MANUFACTURER,
            name=entry.title,
        )

        options = entry.options
        supports_color = options.get(CONF_SUPPORTS_COLOR, DEFAULT_SUPPORTS_COLOR)
        self._attr_supported_color_modes = {ColorMode.COLOR_TEMP}
        if supports_color:
            self._attr_supported_color_modes.add(ColorMode.HS)
        self._attr_min_color_temp_kelvin = int(
            options.get(CONF_MIN_KELVIN, DEFAULT_MIN_KELVIN)
        )
        self._attr_max_color_temp_kelvin = int(
            options.get(CONF_MAX_KELVIN, DEFAULT_MAX_KELVIN)
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
    def is_on(self) -> bool | None:
        state = self._device.state
        return None if state is None else state.on

    @property
    def brightness(self) -> int | None:
        state = self._device.state
        return None if state is None else _to_brightness(state.intensity)

    @property
    def color_mode(self) -> ColorMode | None:
        state = self._device.state
        if state is None:
            return None
        if state.is_hsi and ColorMode.HS in self._attr_supported_color_modes:
            return ColorMode.HS
        return ColorMode.COLOR_TEMP

    @property
    def color_temp_kelvin(self) -> int | None:
        state = self._device.state
        if state is None or state.is_hsi:
            return None
        return min(
            max(state.kelvin, self._attr_min_color_temp_kelvin),
            self._attr_max_color_temp_kelvin,
        )

    @property
    def hs_color(self) -> tuple[float, float] | None:
        state = self._device.state
        if state is None or not state.is_hsi:
            return None
        return (float(state.hue), float(state.saturation))

    async def async_turn_on(self, **kwargs: Any) -> None:
        state = self._device.state
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        color_temp = kwargs.get(ATTR_COLOR_TEMP_KELVIN)
        hs_color = kwargs.get(ATTR_HS_COLOR)

        if brightness is not None:
            intensity = _to_intensity(brightness)
        elif state is not None and state.intensity > 0:
            intensity = state.intensity
        else:
            intensity = MAX_INTENSITY

        try:
            # Colour and CCT messages carry intensity, so one message covers
            # both; a plain brightness change keeps whichever mode is active.
            if hs_color is not None:
                await self._device.async_set_hsi(hs_color[0], hs_color[1], intensity)
            elif color_temp is not None:
                color_temp = min(
                    max(color_temp, self._attr_min_color_temp_kelvin),
                    self._attr_max_color_temp_kelvin,
                )
                gm = state.gm if state is not None and not state.is_hsi else 0
                await self._device.async_set_cct(color_temp, intensity, gm)
            elif brightness is not None:
                await self._device.async_set_brightness(intensity)

            # Setting parameters does not wake a sleeping fixture, so power on
            # last -- that way it never flashes at the previous settings.
            if state is None or not state.on:
                await self._device.async_turn_on()

            await self._device.async_refresh_state()
        except AmaranConnectionError as err:
            raise HomeAssistantError(str(err)) from err

    async def async_turn_off(self, **kwargs: Any) -> None:
        try:
            await self._device.async_turn_off()
            await self._device.async_refresh_state()
        except AmaranConnectionError as err:
            raise HomeAssistantError(str(err)) from err
