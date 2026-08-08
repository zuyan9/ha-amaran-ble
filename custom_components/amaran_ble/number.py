"""Number platform for optional amaran fixture controls."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import CONF_ADDRESS, Platform
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
)
from .device import AmaranConnectionError, AmaranLight

PARALLEL_UPDATES = 1


def _gm_unique_id(address: str) -> str:
    return f"{address}_gm"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AmaranConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up G/M when the fixture advertises that capability."""
    options = entry.options
    supports_gm = bool(
        options.get(CONF_SUPPORTS_CCT, DEFAULT_SUPPORTS_CCT)
        and options.get(CONF_SUPPORTS_GM, DEFAULT_SUPPORTS_GM)
    )
    if supports_gm:
        async_add_entities([AmaranGreenMagentaEntity(entry)])
        return

    # Disabling the capability should remove the old slider rather than leave
    # an unavailable orphan in the entity registry after the entry reloads.
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        Platform.NUMBER, DOMAIN, _gm_unique_id(entry.data[CONF_ADDRESS])
    )
    if entity_id is not None:
        registry.async_remove(entity_id)


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
