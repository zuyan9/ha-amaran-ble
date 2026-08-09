"""Helpers for replacing Mesh credentials without replacing HA identity."""

from __future__ import annotations

import hmac
from collections.abc import Callable, Mapping
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.core import HomeAssistant, callback

from .amaranble.gatt import MESH_PROVISIONING_SERVICE, MESH_PROXY_SERVICE
from .amaranble.network import NetworkKeys
from .const import (
    CONF_APP_KEY,
    CONF_DEVICE_KEY,
    CONF_NET_KEY,
    CONF_TRANSPORT_ADDRESS,
    CONF_UNICAST_ADDRESS,
    NODE_ADDRESS,
)
from .pending import async_get_pending


def _service_data(info: BluetoothServiceInfoBleak, service_uuid: str) -> bytes | None:
    """Return one service-data field without relying on UUID key case."""
    wanted = service_uuid.casefold()
    return next(
        (
            bytes(data)
            for uuid, data in info.service_data.items()
            if uuid.casefold() == wanted
        ),
        None,
    )


def _stored_key(data: Mapping[str, Any], key: str) -> bytes | None:
    """Decode one complete stored Mesh key without trusting its shape."""
    value = data.get(key)
    if not isinstance(value, str):
        return None
    try:
        decoded = bytes.fromhex(value)
    except ValueError:
        return None
    return decoded if len(decoded) == 16 else None


async def _pending_replacement_identity(
    hass: HomeAssistant, entry: ConfigEntry
) -> tuple[NetworkKeys, int] | None:
    """Return pending subnet identity only for a real replacement transaction."""
    stable_address = entry.data.get(CONF_ADDRESS)
    if not isinstance(stable_address, str):
        return None
    try:
        record = await async_get_pending(hass, stable_address)
    except AttributeError, OSError, TypeError, ValueError:
        # A missing or unreadable recovery copy must not hide a genuine reset
        # advertisement. It only makes Proxy recovery ineligible.
        return None
    if not isinstance(record, Mapping):
        return None
    pending = record.get("data")
    if not isinstance(pending, Mapping):
        return None

    current_net_key = _stored_key(entry.data, CONF_NET_KEY)
    current_app_key = _stored_key(entry.data, CONF_APP_KEY)
    current_device_key = _stored_key(entry.data, CONF_DEVICE_KEY)
    pending_net_key = _stored_key(pending, CONF_NET_KEY)
    pending_app_key = _stored_key(pending, CONF_APP_KEY)
    pending_device_key = _stored_key(pending, CONF_DEVICE_KEY)
    if (
        current_net_key is None
        or current_app_key is None
        or current_device_key is None
        or pending_net_key is None
        or pending_app_key is None
        or pending_device_key is None
        or not hmac.compare_digest(current_net_key, pending_net_key)
        or not hmac.compare_digest(current_app_key, pending_app_key)
        or hmac.compare_digest(current_device_key, pending_device_key)
    ):
        return None

    current_unicast = entry.data.get(CONF_UNICAST_ADDRESS, NODE_ADDRESS)
    pending_unicast = pending.get(CONF_UNICAST_ADDRESS, NODE_ADDRESS)
    if (
        not isinstance(current_unicast, int)
        or isinstance(current_unicast, bool)
        or not 0x0001 <= current_unicast <= 0x7FFF
        or pending_unicast != current_unicast
    ):
        return None
    return NetworkKeys.derive(pending_net_key), pending_unicast


async def async_reprovision_candidates(
    hass: HomeAssistant,
    entry: ConfigEntry,
    is_fixture: Callable[[BluetoothServiceInfoBleak], bool],
) -> dict[str, BluetoothServiceInfoBleak]:
    """Find reset fixtures plus authenticated proxies from an interrupted fix.

    After Provisioning Complete the selected address stops advertising the
    provisioning bearer. If Home Assistant stopped before updating the entry,
    its durable pending record and unchanged NetKey can still identify that
    proxy cryptographically, allowing the repair to resume instead of becoming
    stranded behind a provisioning-only candidate filter.
    """
    replacement_identity = await _pending_replacement_identity(hass, entry)

    persisted_addresses = {
        value.casefold()
        for value in (
            entry.data.get(CONF_ADDRESS),
            entry.data.get(CONF_TRANSPORT_ADDRESS),
        )
        if isinstance(value, str)
    }
    candidates: dict[str, BluetoothServiceInfoBleak] = {}
    for info in bluetooth.async_discovered_service_info(hass, connectable=True):
        provisioning = _service_data(info, MESH_PROVISIONING_SERVICE)
        proxy = _service_data(info, MESH_PROXY_SERVICE)
        # Discovery caches are additive, so a genuine reset can retain a stale
        # Proxy page beside its fresh provisioning page. Keep it eligible here;
        # the uncached GATT probe before mutation resolves the live bearer.
        if provisioning is not None and (
            is_fixture(info) or info.address.casefold() in persisted_addresses
        ):
            candidates[info.address] = info
            continue
        if (
            proxy is not None
            and replacement_identity is not None
            and replacement_identity[0].proxy_identity_match(
                proxy, replacement_identity[1]
            )
            is not None
        ):
            candidates[info.address] = info
    return candidates


def reprovisioned_entry_data(
    entry: ConfigEntry, provisioned: dict[str, Any]
) -> dict[str, Any]:
    """Merge new Mesh credentials while retaining stable registry identifiers.

    The first Bluetooth address is already part of the config-entry unique ID,
    every entity unique ID, and the device-registry identifier. Re-provisioning
    can discover the same physical fixture under a different transport address;
    changing ``CONF_ADDRESS`` would duplicate all of those Home Assistant
    objects. Keep it stable and persist the newly observed route separately.
    """
    stable_address = entry.data[CONF_ADDRESS]
    transport_address = provisioned[CONF_ADDRESS]

    updated = {**entry.data, **provisioned, CONF_ADDRESS: stable_address}
    if original_name := entry.data.get(CONF_NAME):
        updated[CONF_NAME] = original_name

    if transport_address.casefold() == stable_address.casefold():
        updated.pop(CONF_TRANSPORT_ADDRESS, None)
    else:
        updated[CONF_TRANSPORT_ADDRESS] = transport_address
    return updated


@callback
def async_update_reprovisioned_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    provisioned: dict[str, Any],
) -> None:
    """Update repaired credentials and arrange exactly one prompt reload.

    Loaded entries already register an update listener that reloads them. An
    entry whose setup failed has no listener, while an unchanged-but-retryable
    entry still needs an explicit reload. Cover those cases without scheduling
    a second reload alongside the listener.
    """
    changed = hass.config_entries.async_update_entry(
        entry,
        data=reprovisioned_entry_data(entry, provisioned),
    )
    if not changed or not entry.update_listeners:
        hass.config_entries.async_schedule_reload(entry.entry_id)
