"""The amaran BLE integration.

Controls amaran / Aputure studio fixtures over Bluetooth Mesh, entirely
locally. Each config entry provisions its fixture into a private mesh that
Home Assistant creates, so no vendor app or cloud account is involved.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from collections.abc import Mapping

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
    CONF_SEQUENCE_STORE_ID,
    CONF_SUPPORTS_COLOR,
    CONF_TRANSPORT_ADDRESS,
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
from .pending import async_get_pending, async_remove_pending
from .profiles import profile_for_entry
from .reconfiguration import reprovisioned_entry_data
from .repairs import (
    async_create_factory_reset_issue,
    async_delete_factory_reset_issue,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.LIGHT,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

# Resetting the node on removal is best effort; never let it stall the UI.
RELEASE_TIMEOUT = 30.0

type AmaranConfigEntry = ConfigEntry[AmaranLight]


def _decode_stored_key(data: Mapping[str, object], key: str) -> bytes | None:
    """Decode one complete 128-bit stored key without trusting its shape."""
    try:
        value = data[key]
        decoded = bytes.fromhex(value) if isinstance(value, str) else b""
    except ValueError:
        return None
    return decoded if len(decoded) == 16 else None


async def _async_reconcile_completed_repair(
    hass: HomeAssistant, entry: AmaranConfigEntry
) -> None:
    """Recover a repair committed before its config-entry update reached disk.

    Pending credentials are synchronously persisted before Mesh Provisioning
    Data, while Core deliberately delays config-entry writes.  Following an
    abrupt restart, a newer DeviceKey in the stable pending record is therefore
    authoritative only when it belongs to the entry's existing private subnet.
    """
    stable_address = entry.data[CONF_ADDRESS]
    record = await async_get_pending(hass, stable_address)
    if record is None:
        return
    raw_pending = record.get("data")
    if not isinstance(raw_pending, Mapping):
        return
    pending = dict(raw_pending)

    current_net_key = _decode_stored_key(entry.data, CONF_NET_KEY)
    pending_net_key = _decode_stored_key(pending, CONF_NET_KEY)
    current_app_key = _decode_stored_key(entry.data, CONF_APP_KEY)
    pending_app_key = _decode_stored_key(pending, CONF_APP_KEY)
    current_device_key = _decode_stored_key(entry.data, CONF_DEVICE_KEY)
    pending_device_key = _decode_stored_key(pending, CONF_DEVICE_KEY)
    same_subnet = (
        current_net_key is not None
        and pending_net_key is not None
        and hmac.compare_digest(current_net_key, pending_net_key)
        and current_app_key is not None
        and pending_app_key is not None
        and hmac.compare_digest(current_app_key, pending_app_key)
    )
    if (
        not same_subnet
        or current_device_key is None
        or pending_device_key is None
        or hmac.compare_digest(current_device_key, pending_device_key)
    ):
        return

    if record.get("committed") is not True:
        # The pre-DATA hook ran, but neither the old config entry nor this
        # uncertain replacement DeviceKey is safe to use.  Preserve both and
        # leave the authenticated Repair flow to resolve the live Proxy bearer.
        async_create_factory_reset_issue(hass, entry)
        raise ConfigEntryNotReady(
            f"Re-provisioning {entry.title} was interrupted. Resume its repair flow."
        )

    if not isinstance(pending.get(CONF_ADDRESS), str):
        # A different DeviceKey means the old entry can no longer be trusted,
        # but an incomplete record cannot safely identify the replacement BLE
        # route either. Keep both copies and require an authenticated repair.
        async_create_factory_reset_issue(hass, entry)
        raise ConfigEntryNotReady(
            f"Re-provisioning recovery data for {entry.title} is incomplete. "
            "Resume its repair flow."
        )

    pending[CONF_NEEDS_CONFIGURATION] = True
    updated = reprovisioned_entry_data(entry, pending)
    hass.config_entries.async_update_entry(entry, data=updated)
    _LOGGER.info(
        "recovered completed re-provisioning credentials for %s from durable state",
        entry.title,
    )


async def async_migrate_entry(hass: HomeAssistant, entry: AmaranConfigEntry) -> bool:
    """Migrate config entries created by the pre-release prototype."""
    if entry.version > 1 or (entry.version == 1 and entry.minor_version > 3):
        return False

    # A pre-release 0.3 build briefly used minor version 3 for additive option
    # keys. Unknown options are safely ignored by 0.3.1, so normalize it back
    # to 2 and retain HACS rollback compatibility.
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
    await _async_reconcile_completed_repair(hass, entry)
    data = entry.data
    if CONF_SEQUENCE_STORE_ID not in data:
        # Legacy entries already used entry_id as their sequence-store key.
        # Recording that fallback keeps the key explicit without abandoning
        # the existing high-water mark.
        updated = dict(data)
        updated[CONF_SEQUENCE_STORE_ID] = entry.entry_id
        hass.config_entries.async_update_entry(entry, data=updated)
        data = entry.data
    sequence_store_id = data[CONF_SEQUENCE_STORE_ID]
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
                sequence_store_id=sequence_store_id,
                compatibility_sequence_store_id=entry.entry_id,
                transport_address=data.get(CONF_TRANSPORT_ADDRESS),
            )
        except NodeConfigurationError as err:
            updated = dict(data)
            updated[CONF_INITIAL_SEQUENCE] = err.sequence
            hass.config_entries.async_update_entry(entry, data=updated)
            raise ConfigEntryNotReady(
                f"Finishing Bluetooth Mesh configuration for {entry.title} failed: {err}"
            ) from err
        except AmaranNotProvisionedError as err:
            async_create_factory_reset_issue(hass, entry)
            raise ConfigEntryNotReady(
                f"{entry.title} has been factory reset and is no longer part of Home "
                "Assistant's mesh. Use the repair flow to re-provision it in place."
            ) from err
        except AmaranConnectionError as err:
            raise ConfigEntryNotReady(str(err)) from err

        updated = dict(data)
        updated[CONF_INITIAL_SEQUENCE] = initial_sequence
        updated.pop(CONF_NEEDS_CONFIGURATION, None)
        hass.config_entries.async_update_entry(entry, data=updated)
        # Configuration uses the DeviceKey and finishes with an authenticated
        # primary Telink status reply, so it is stronger membership proof than
        # merely opening a GATT Proxy bearer.
        async_delete_factory_reset_issue(hass, entry.entry_id)
        data = entry.data

    device = AmaranLight(
        hass,
        sequence_store_id,
        data[CONF_ADDRESS],
        data.get(CONF_NAME) or entry.title,
        compatibility_sequence_store_id=entry.entry_id,
        net_key=bytes.fromhex(data[CONF_NET_KEY]),
        app_key=bytes.fromhex(data[CONF_APP_KEY]),
        device_key=bytes.fromhex(data[CONF_DEVICE_KEY]),
        unicast_address=data.get(CONF_UNICAST_ADDRESS, NODE_ADDRESS),
        local_address=data.get(CONF_LOCAL_ADDRESS, PROVISIONER_ADDRESS),
        iv_index=data.get(CONF_IV_INDEX, 0),
        initial_sequence=data.get(CONF_INITIAL_SEQUENCE, 0),
        profile=profile_for_entry(entry),
        transport_address=data.get(CONF_TRANSPORT_ADDRESS),
        on_not_provisioned=lambda: async_create_factory_reset_issue(hass, entry),
        on_provisioned=lambda: async_delete_factory_reset_issue(hass, entry.entry_id),
    )

    try:
        try:
            await device.async_start()
        except AmaranNotProvisionedError as err:
            async_create_factory_reset_issue(hass, entry)
            raise ConfigEntryNotReady(
                f"{entry.title} has been factory reset and is no longer part of Home "
                "Assistant's mesh. Use the repair flow to re-provision it in place."
            ) from err
        except AmaranConnectionError as err:
            raise ConfigEntryNotReady(str(err)) from err

        entry.runtime_data = device
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
    async_delete_factory_reset_issue(hass, entry.entry_id)
    sequence_store_id = data.get(CONF_SEQUENCE_STORE_ID, entry.entry_id)
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
                sequence_store_id=sequence_store_id,
                compatibility_sequence_store_id=entry.entry_id,
                minimum_sequence=data.get(CONF_INITIAL_SEQUENCE, 0),
                transport_address=data.get(CONF_TRANSPORT_ADDRESS),
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

    for store_id in dict.fromkeys((sequence_store_id, entry.entry_id)):
        try:
            await Store(hass, 1, f"{DOMAIN}.{store_id}").async_remove()
        except Exception as err:  # Entry deletion must not depend on cache cleanup.
            _LOGGER.warning(
                "could not remove sequence state for %s: %s",
                entry.title,
                err,
            )
