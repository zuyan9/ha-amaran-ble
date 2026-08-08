"""Switch platform for amaran BLE fixtures."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AmaranConfigEntry
from .const import DOMAIN, MANUFACTURER
from .device import AmaranConnectionError, AmaranLight
from .profiles import profile_for_entry

PARALLEL_UPDATES = 1


def _boost_unique_id(address: str) -> str:
    return f"{address}_boost"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AmaranConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Boost for fixtures with a verified Boost command."""
    if profile_for_entry(entry).supports_boost:
        async_add_entities([AmaranBoostEntity(entry)])
        return

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        Platform.SWITCH, DOMAIN, _boost_unique_id(entry.data[CONF_ADDRESS])
    )
    if entity_id is not None:
        registry.async_remove(entity_id)


class AmaranBoostEntity(SwitchEntity):
    """Enable the Ace 25x high-output Boost mode."""

    _attr_has_entity_name = True
    _attr_translation_key = "boost"
    _attr_icon = "mdi:rocket-launch"
    _attr_should_poll = False

    def __init__(self, entry: AmaranConfigEntry) -> None:
        self._device: AmaranLight = entry.runtime_data
        address = entry.data[CONF_ADDRESS]
        self._attr_unique_id = _boost_unique_id(address)
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
        return self._device.connected and self._device.boost_state is not None

    @property
    def is_on(self) -> bool | None:
        state = self._device.boost_state
        return None if state is None else state.enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        try:
            await self._device.async_set_boost(True)
        except AmaranConnectionError as err:
            raise HomeAssistantError(str(err)) from err

    async def async_turn_off(self, **kwargs: Any) -> None:
        try:
            await self._device.async_set_boost(False)
        except AmaranConnectionError as err:
            raise HomeAssistantError(str(err)) from err
