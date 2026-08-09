"""End-to-end Home Assistant config-entry lifecycle coverage."""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.light import ATTR_BRIGHTNESS
from homeassistant.components.light import DOMAIN as LIGHT_DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    CONF_ADDRESS,
    CONF_NAME,
    SERVICE_TURN_ON,
    STATE_UNAVAILABLE,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.amaran_ble.amaranble.telink import LightState
from custom_components.amaran_ble.const import (
    CONF_APP_KEY,
    CONF_DEVICE_KEY,
    CONF_INITIAL_SEQUENCE,
    CONF_IV_INDEX,
    CONF_LOCAL_ADDRESS,
    CONF_MODEL,
    CONF_NET_KEY,
    CONF_NUM_ELEMENTS,
    CONF_SEQUENCE_STORE_ID,
    CONF_UNICAST_ADDRESS,
    DOMAIN,
    PROFILE_GENERIC,
)
from custom_components.amaran_ble.profiles import GENERIC_PROFILE

ADDRESS = "AA:BB:CC:DD:EE:FF"


def _entry_data() -> dict[str, object]:
    """Return a complete persisted mesh entry without exposing real keys."""
    return {
        CONF_ADDRESS: ADDRESS,
        CONF_NAME: "Test light",
        CONF_NET_KEY: "01" * 16,
        CONF_APP_KEY: "02" * 16,
        CONF_DEVICE_KEY: "03" * 16,
        CONF_UNICAST_ADDRESS: 2,
        CONF_LOCAL_ADDRESS: 1,
        CONF_NUM_ELEMENTS: 1,
        CONF_IV_INDEX: 0,
        CONF_INITIAL_SEQUENCE: 0,
        CONF_SEQUENCE_STORE_ID: "test-sequence-store",
    }


class FakeAmaranLight:
    """Small runtime double used through HA's real entity platform machinery."""

    def __init__(self) -> None:
        self.profile = GENERIC_PROFILE
        self.state = LightState(
            on=False,
            is_hsi=False,
            intensity=200,
            kelvin=4500,
            gm=0,
            hue=0,
            saturation=0,
        )
        self.effect_state = None
        self.available = True
        self.connected = True
        self.async_start = AsyncMock()
        self.async_stop = AsyncMock()
        self.async_apply_turn_on = AsyncMock()
        self.async_apply_turn_off = AsyncMock()
        self._listeners: list[Callable[[], None]] = []

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Register the entity update callback."""
        self._listeners.append(listener)

        def remove() -> None:
            self._listeners.remove(listener)

        return remove


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_config_entry_setup_service_reload_unload_and_remove(
    hass: HomeAssistant,
) -> None:
    """Exercise the integration through HA's real config-entry and entity APIs."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test light",
        unique_id=ADDRESS,
        data=_entry_data(),
        options={CONF_MODEL: PROFILE_GENERIC},
        minor_version=2,
    )
    entry.add_to_hass(hass)
    first, second, third = (FakeAmaranLight() for _ in range(3))

    with (
        patch(
            "custom_components.amaran_ble.AmaranLight",
            side_effect=(first, second, third),
        ) as light_factory,
        patch(
            "custom_components.amaran_ble.async_release_node",
            new=AsyncMock(return_value=True),
        ) as release_node,
        patch(
            "custom_components.amaran_ble.async_remove_pending", new=AsyncMock()
        ) as remove_pending,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        first.async_start.assert_awaited_once_with()

        entity_id = er.async_get(hass).async_get_entity_id(
            Platform.LIGHT, DOMAIN, ADDRESS
        )
        assert entity_id is not None
        assert hass.states.get(entity_id) is not None

        await hass.services.async_call(
            LIGHT_DOMAIN,
            SERVICE_TURN_ON,
            {"entity_id": entity_id, ATTR_BRIGHTNESS: 128},
            blocking=True,
        )
        first.async_apply_turn_on.assert_awaited_once_with(
            intensity=502,
            brightness_changed=True,
            kelvin=None,
            hs_color=None,
        )

        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        first.async_stop.assert_awaited_once_with()
        second.async_start.assert_awaited_once_with()
        assert entry.state is ConfigEntryState.LOADED

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        second.async_stop.assert_awaited_once_with()
        assert entry.state is ConfigEntryState.NOT_LOADED
        # HA keeps a restored placeholder while the config entry is unloaded.
        unloaded_state = hass.states.get(entity_id)
        assert unloaded_state is not None
        assert unloaded_state.state == STATE_UNAVAILABLE

        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        third.async_start.assert_awaited_once_with()
        assert entry.state is ConfigEntryState.LOADED

        assert await hass.config_entries.async_remove(entry.entry_id) == {
            "require_restart": False
        }
        await hass.async_block_till_done()

    third.async_stop.assert_awaited_once_with()
    release_node.assert_awaited_once()
    remove_pending.assert_awaited_once_with(hass, ADDRESS)
    assert hass.config_entries.async_get_entry(entry.entry_id) is None
    assert light_factory.call_count == 3


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_failed_setup_stops_partial_runtime_and_enters_retry(
    hass: HomeAssistant,
) -> None:
    """A failed first connection must clean up before HA schedules a retry."""
    from custom_components.amaran_ble.device import AmaranConnectionError

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test light",
        unique_id=ADDRESS,
        data=_entry_data(),
        options={CONF_MODEL: PROFILE_GENERIC},
        minor_version=2,
    )
    entry.add_to_hass(hass)
    runtime = FakeAmaranLight()
    runtime.async_start.side_effect = AmaranConnectionError("not in range")

    with patch("custom_components.amaran_ble.AmaranLight", return_value=runtime):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    runtime.async_stop.assert_awaited_once_with()
    assert entry.state is ConfigEntryState.SETUP_RETRY
