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


def _high_speed_unique_id(address: str) -> str:
    return f"{address}_high_speed_photography"


def _remove_registered_entity(hass: HomeAssistant, unique_id: str) -> None:
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(Platform.SWITCH, DOMAIN, unique_id)
    if entity_id is not None:
        registry.async_remove(entity_id)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AmaranConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up profile-gated proprietary fixture modes."""
    profile = profile_for_entry(entry)
    address = entry.data[CONF_ADDRESS]
    entities: list[SwitchEntity] = []
    if profile.supports_boost:
        entities.append(AmaranBoostEntity(entry))
    else:
        _remove_registered_entity(hass, _boost_unique_id(address))

    if profile.catalog_capabilities.high_speed_photography.supported:
        entities.append(AmaranHighSpeedPhotographyEntity(entry))
    else:
        _remove_registered_entity(hass, _high_speed_unique_id(address))

    if entities:
        async_add_entities(entities)


class AmaranBoostEntity(SwitchEntity):
    """Enable the Ace 25x high-output Boost mode."""

    _attr_has_entity_name = True
    # Command 70 is write-only. The fixture does not provide a trustworthy
    # enabled-state report, so this entity reflects the last successful local
    # request rather than an independently confirmed hardware state.
    _attr_assumed_state = True
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


class AmaranHighSpeedPhotographyEntity(SwitchEntity):
    """Enable the fixture's cataloged high-speed-photography mode."""

    _attr_has_entity_name = True
    _attr_translation_key = "high_speed_photography"
    _attr_icon = "mdi:camera-burst"
    _attr_should_poll = False
    # Command 53 has no proven read or acknowledgement path. The cached value
    # is the last requested state (or an unsolicited packet with unknown
    # direction), so Home Assistant must not present it as authoritative.
    _attr_assumed_state = True

    def __init__(self, entry: AmaranConfigEntry) -> None:
        self._device: AmaranLight = entry.runtime_data
        address = entry.data[CONF_ADDRESS]
        self._attr_unique_id = _high_speed_unique_id(address)
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
        # Command 53 has no separate state query in the app artifact, so the
        # control remains usable while connected even before its first write.
        return self._device.connected

    @property
    def is_on(self) -> bool | None:
        state = self._device.high_speed_state
        return None if state is None else state.enabled

    @property
    def extra_state_attributes(self) -> dict[str, int]:
        capability = self._device.profile.catalog_capabilities.high_speed_photography
        return {
            "minimum_intensity_percent": capability.intensity_min,
            "maximum_intensity_percent": capability.intensity_max,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        try:
            await self._device.async_set_high_speed(True)
        except AmaranConnectionError as err:
            raise HomeAssistantError(str(err)) from err

    async def async_turn_off(self, **kwargs: Any) -> None:
        try:
            await self._device.async_set_high_speed(False)
        except AmaranConnectionError as err:
            raise HomeAssistantError(str(err)) from err
