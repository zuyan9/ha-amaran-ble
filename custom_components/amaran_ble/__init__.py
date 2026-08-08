"""The amaran BLE integration.

Controls amaran / Aputure studio fixtures over Bluetooth Mesh, entirely
locally. Each config entry provisions its fixture into a private mesh that
Home Assistant creates, so no vendor app or cloud account is involved.
"""

from __future__ import annotations

import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, CONF_NAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.storage import Store

from .const import (
    CONF_APP_KEY,
    CONF_DEVICE_KEY,
    CONF_INITIAL_SEQUENCE,
    CONF_IV_INDEX,
    CONF_LOCAL_ADDRESS,
    CONF_MAX_KELVIN,
    CONF_MIN_KELVIN,
    CONF_NEEDS_CONFIGURATION,
    CONF_NET_KEY,
    CONF_SUPPORTS_COLOR,
    CONF_UNICAST_ADDRESS,
    DEFAULT_MAX_KELVIN,
    DEFAULT_MIN_KELVIN,
    DOMAIN,
    NODE_ADDRESS,
    PROVISIONER_ADDRESS,
)
from .device import (
    AmaranConnectionError,
    AmaranLight,
    AmaranNotProvisionedError,
    NodeConfigurationError,
    async_configure_stored_node,
    async_release_node,
)
from .pending import async_remove_pending

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.LIGHT, Platform.NUMBER]

# Resetting the node on removal is best effort; never let it stall the UI.
RELEASE_TIMEOUT = 30.0

type AmaranConfigEntry = ConfigEntry[AmaranLight]


async def async_migrate_entry(hass: HomeAssistant, entry: AmaranConfigEntry) -> bool:
    """Migrate config entries created by the pre-release prototype."""
    if entry.version > 1 or (entry.version == 1 and entry.minor_version > 3):
        return False

    # A pre-release 0.3 build briefly used minor version 3 for additive option
    # keys. The old integration already ignores unknown options, so normalize
    # it back to 2 and retain safe HACS rollback compatibility.
    if entry.version == 1 and entry.minor_version == 3:
        hass.config_entries.async_update_entry(entry, minor_version=2)
        return True

    if entry.version == 1 and entry.minor_version < 2:
        options = dict(entry.options)
        # The prototype offered 2500-7500 K as its generic defaults. Narrowly
        # update that exact bi-colour default to the hardware-verified Ace 25x
        # range without changing a user's custom range or a full-colour light.
        if (
            not options.get(CONF_SUPPORTS_COLOR, False)
            and options.get(CONF_MIN_KELVIN) == 2500
            and options.get(CONF_MAX_KELVIN) == 7500
        ):
            options[CONF_MIN_KELVIN] = DEFAULT_MIN_KELVIN
            options[CONF_MAX_KELVIN] = DEFAULT_MAX_KELVIN

        hass.config_entries.async_update_entry(
            entry,
            version=1,
            minor_version=2,
            options=options,
        )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: AmaranConfigEntry) -> bool:
    """Connect to the fixture and set up its light entity."""
    data = entry.data
    if data.get(CONF_NEEDS_CONFIGURATION):
        try:
            initial_sequence = await async_configure_stored_node(
                hass,
                data[CONF_ADDRESS],
                data.get(CONF_NAME) or entry.title,
                net_key=bytes.fromhex(data[CONF_NET_KEY]),
                app_key=bytes.fromhex(data[CONF_APP_KEY]),
                device_key=bytes.fromhex(data[CONF_DEVICE_KEY]),
                unicast_address=data.get(CONF_UNICAST_ADDRESS, NODE_ADDRESS),
                local_address=data.get(CONF_LOCAL_ADDRESS, PROVISIONER_ADDRESS),
                iv_index=data.get(CONF_IV_INDEX, 0),
                sequence=data.get(CONF_INITIAL_SEQUENCE, 0),
                entry_id=entry.entry_id,
            )
        except NodeConfigurationError as err:
            updated = dict(data)
            updated[CONF_INITIAL_SEQUENCE] = err.sequence
            hass.config_entries.async_update_entry(entry, data=updated)
            raise ConfigEntryNotReady(
                f"Finishing Bluetooth Mesh configuration for {entry.title} failed: {err}"
            ) from err
        except AmaranConnectionError as err:
            raise ConfigEntryNotReady(str(err)) from err

        updated = dict(data)
        updated[CONF_INITIAL_SEQUENCE] = initial_sequence
        updated.pop(CONF_NEEDS_CONFIGURATION, None)
        hass.config_entries.async_update_entry(entry, data=updated)
        data = entry.data

    device = AmaranLight(
        hass,
        entry.entry_id,
        data[CONF_ADDRESS],
        data.get(CONF_NAME) or entry.title,
        net_key=bytes.fromhex(data[CONF_NET_KEY]),
        app_key=bytes.fromhex(data[CONF_APP_KEY]),
        device_key=bytes.fromhex(data[CONF_DEVICE_KEY]),
        unicast_address=data.get(CONF_UNICAST_ADDRESS, NODE_ADDRESS),
        local_address=data.get(CONF_LOCAL_ADDRESS, PROVISIONER_ADDRESS),
        iv_index=data.get(CONF_IV_INDEX, 0),
        initial_sequence=data.get(CONF_INITIAL_SEQUENCE, 0),
    )

    try:
        await device.async_start()
    except AmaranNotProvisionedError as err:
        await device.async_stop()
        raise ConfigEntryNotReady(
            f"{entry.title} has been factory reset and is no longer part of Home "
            "Assistant's mesh. Delete this device and add it again to re-provision it."
        ) from err
    except AmaranConnectionError as err:
        await device.async_stop()
        raise ConfigEntryNotReady(str(err)) from err

    entry.runtime_data = device
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        await device.async_stop()
        raise
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AmaranConfigEntry) -> bool:
    """Disconnect and tear down."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_stop()
    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: AmaranConfigEntry) -> None:
    """Apply changed options."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_entry(hass: HomeAssistant, entry: AmaranConfigEntry) -> None:
    """Hand the fixture back before forgetting its keys.

    Removal is the last moment we can reach the node, so reset it here: that
    returns it to the unprovisioned state where Home Assistant -- or the amaran
    app -- can adopt it again. If the light is off or out of range this cannot
    be done, and the user has to reset it at the fixture instead; either way
    removal itself must still succeed.
    """
    data = entry.data
    try:
        async with asyncio.timeout(RELEASE_TIMEOUT):
            released = await async_release_node(
                hass,
                data[CONF_ADDRESS],
                net_key=bytes.fromhex(data[CONF_NET_KEY]),
                app_key=bytes.fromhex(data[CONF_APP_KEY]),
                device_key=bytes.fromhex(data[CONF_DEVICE_KEY]),
                unicast_address=data.get(CONF_UNICAST_ADDRESS, NODE_ADDRESS),
                local_address=data.get(CONF_LOCAL_ADDRESS, PROVISIONER_ADDRESS),
                iv_index=data.get(CONF_IV_INDEX, 0),
                entry_id=entry.entry_id,
                minimum_sequence=data.get(CONF_INITIAL_SEQUENCE, 0),
            )
    except Exception as err:  # Removal must never block config-entry deletion.
        released = False
        _LOGGER.debug("could not reset %s on removal: %s", data[CONF_ADDRESS], err)

    if not released:
        _LOGGER.warning(
            "%s could not be reached to be reset, so it still belongs to Home "
            "Assistant's mesh. Factory reset the fixture before adding it again "
            "or pairing it with the amaran app",
            entry.title,
        )

    # Config-entry setup can run before Home Assistant flushes core.config_entries,
    # so the pre-commit copy must remain throughout the entry's lifetime. The
    # configured unique ID prevents it from being offered as a recovery flow.
    # Removal is the first lifecycle point where retaining it is no longer useful.
    try:
        await async_remove_pending(hass, data[CONF_ADDRESS])
    except Exception as err:  # Removal must never block config-entry deletion.
        # Entry deletion must still succeed. A stale private recovery record is
        # safer than trapping an entry the user explicitly asked to remove.
        _LOGGER.warning(
            "could not remove stale provisioning recovery data for %s: %s",
            entry.title,
            err,
        )

    await Store(hass, 1, f"{DOMAIN}.{entry.entry_id}").async_remove()
