"""Helpers for replacing Mesh credentials without replacing HA identity."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.core import HomeAssistant, callback

from .amaranble.gatt import MESH_PROVISIONING_SERVICE, MESH_PROXY_SERVICE
from .amaranble.network import NetworkKeys
from .const import (
    CONF_NET_KEY,
    CONF_TRANSPORT_ADDRESS,
    CONF_UNICAST_ADDRESS,
    NODE_ADDRESS,
)


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


def async_reprovision_candidates(
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
    try:
        net_key = bytes.fromhex(entry.data[CONF_NET_KEY])
        unicast_address = entry.data.get(CONF_UNICAST_ADDRESS, NODE_ADDRESS)
        if (
            len(net_key) != 16
            or not isinstance(unicast_address, int)
            or isinstance(unicast_address, bool)
            or not 0x0001 <= unicast_address <= 0x7FFF
        ):
            raise ValueError
        network_keys = NetworkKeys.derive(net_key)
    except KeyError, TypeError, ValueError:
        network_keys = None
        unicast_address = NODE_ADDRESS

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
        if provisioning is not None and (
            is_fixture(info) or info.address.casefold() in persisted_addresses
        ):
            candidates[info.address] = info
            continue
        if (
            proxy is not None
            and network_keys is not None
            and network_keys.proxy_identity_match(proxy, unicast_address) is not None
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
