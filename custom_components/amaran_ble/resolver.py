"""Resolve a provisioned Mesh node independently of its BLE address."""

from __future__ import annotations

from dataclasses import dataclass

from bleak.backends.device import BLEDevice
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.core import HomeAssistant, callback

from .amaranble.gatt import MESH_PROXY_SERVICE
from .amaranble.network import NetworkKeys, ProxyIdentityMatch


@dataclass(frozen=True, slots=True)
class MeshProxyCandidate:
    """One connectable route that is safe to try for the stored Mesh node."""

    address: str
    ble_device: BLEDevice


@dataclass(frozen=True, slots=True)
class _RankedCandidate:
    """Internal ranking data that never leaves the resolver."""

    candidate: MeshProxyCandidate
    match: ProxyIdentityMatch
    is_hint: bool
    advertisement_time: float


def _same_address(left: str, right: str) -> bool:
    """Compare Bluetooth addresses without assuming a MAC-shaped value."""
    return left.casefold() == right.casefold()


def _service_data(
    info: BluetoothServiceInfoBleak | None, service_uuid: str
) -> bytes | None:
    """Return normalized service data from one Home Assistant advertisement."""
    if info is None:
        return None
    # Home Assistant currently normalizes UUID keys, but accepting case-only
    # differences makes this helper safe for tests and third-party scanners.
    wanted = service_uuid.casefold()
    return next(
        (
            bytes(data)
            for uuid, data in info.service_data.items()
            if uuid.casefold() == wanted
        ),
        None,
    )


@callback
def async_mesh_proxy_candidates(
    hass: HomeAssistant,
    stable_address: str,
    *,
    net_key: bytes,
    unicast_address: int,
    transport_address: str | None = None,
) -> tuple[MeshProxyCandidate, ...]:
    """Return safe connection candidates, preferring a proven transport hint.

    Home Assistant's address lookup chooses the best currently connectable
    scanner source for each candidate. The stored address remains a cautious
    fallback when no identity-bearing service data is available, but a current
    Proxy advertisement that cryptographically belongs to another subnet
    prevents connecting to a recycled random address.

    Every unknown alternate address must prove either this one-node subnet's
    Network ID or the provisioned node's Node Identity before it is returned.
    """
    keys = NetworkKeys.derive(net_key)
    candidates: list[MeshProxyCandidate] = []

    # Both persisted routes are safe to *probe* directly. A Proxy identity for
    # another subnet makes that address unsafe, while missing/provisioning-only
    # cached data remains inconclusive until uncached GATT discovery. Prefer a
    # previously successful alternate so address rotation does not repeatedly
    # spend connector retries on the obsolete stable route.
    known_addresses = [stable_address]
    if transport_address is not None and not _same_address(
        transport_address, stable_address
    ):
        known_addresses.insert(0, transport_address)
    direct_by_address: dict[str, MeshProxyCandidate] = {}
    unsafe_addresses: set[str] = set()
    for address in known_addresses:
        normalized = address.casefold()
        info = bluetooth.async_last_service_info(hass, address, connectable=True)
        proxy_data = _service_data(info, MESH_PROXY_SERVICE)
        if (
            proxy_data is not None
            and keys.proxy_identity_match(proxy_data, unicast_address) is None
        ):
            unsafe_addresses.add(normalized)
            continue
        if ble_device := bluetooth.async_ble_device_from_address(
            hass, address, connectable=True
        ):
            direct_by_address[normalized] = MeshProxyCandidate(address, ble_device)

    # Aggregate discovery can contain a newer identity page than the per-address
    # lookup. Upgrade a direct known probe to an authenticated candidate when it
    # matches, or remove it when current Proxy data proves address reuse.
    # Address comparison is case-insensitive but does not assume MACs;
    # CoreBluetooth and remote scanners can expose UUID-shaped identifiers.
    ranked_by_address: dict[str, _RankedCandidate] = {}
    hint = transport_address or stable_address
    for info in bluetooth.async_discovered_service_info(hass, connectable=True):
        normalized = info.address.casefold()
        if normalized in unsafe_addresses:
            continue
        proxy_data = _service_data(info, MESH_PROXY_SERVICE)
        if proxy_data is None:
            continue
        match = keys.proxy_identity_match(proxy_data, unicast_address)
        if match is None:
            if normalized in direct_by_address or normalized in ranked_by_address:
                direct_by_address.pop(normalized, None)
                ranked_by_address.pop(normalized, None)
                unsafe_addresses.add(normalized)
            continue
        ble_device = bluetooth.async_ble_device_from_address(
            hass, info.address, connectable=True
        )
        if ble_device is None:
            continue
        direct_by_address.pop(normalized, None)
        ranked = _RankedCandidate(
            MeshProxyCandidate(info.address, ble_device),
            match,
            _same_address(info.address, hint),
            info.time,
        )
        previous = ranked_by_address.get(normalized)
        if previous is None or (
            ranked.is_hint,
            ranked.advertisement_time,
            ranked.match,
        ) > (
            previous.is_hint,
            previous.advertisement_time,
            previous.match,
        ):
            ranked_by_address[normalized] = ranked

    ranked_candidates = sorted(
        ranked_by_address.values(),
        key=lambda item: (
            item.is_hint,
            item.advertisement_time,
            item.match,
        ),
        reverse=True,
    )
    # Every authenticated identity wins over address-only hints. When one is
    # available, do not probe unverified stored routes afterward: if the live
    # connection merely fails, a recycled route exposing a provisioning bearer
    # must not turn that transient failure into a false factory-reset Repair.
    if ranked_candidates:
        return tuple(item.candidate for item in ranked_candidates)

    candidates.extend(
        direct_by_address[address.casefold()]
        for address in known_addresses
        if address.casefold() in direct_by_address
    )
    return tuple(candidates)
