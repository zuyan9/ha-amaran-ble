"""Home Assistant Bluetooth Mesh address-resolution tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from custom_components.amaran_ble import resolver
from custom_components.amaran_ble.amaranble import crypto, network
from custom_components.amaran_ble.amaranble.gatt import (
    MESH_PROVISIONING_SERVICE,
    MESH_PROXY_SERVICE,
)

NET_KEY = bytes.fromhex("f7a2a44f8e8a8029064f173ddc1e2b00")
STABLE = "AA:BB:CC:DD:EE:FF"
ALTERNATE = "11:22:33:44:55:66"


def _info(address: str, service_data: dict[str, bytes], time: float):
    """Build only the HA service-info fields consumed by the resolver."""
    return SimpleNamespace(address=address, service_data=service_data, time=time)


def _network_id(net_key: bytes = NET_KEY) -> bytes:
    keys = network.NetworkKeys.derive(net_key)
    return b"\x00" + keys.network_id


def _node_identity(address: int, net_key: bytes = NET_KEY) -> bytes:
    keys = network.NetworkKeys.derive(net_key)
    random_value = bytes.fromhex("1032547698badcfe")
    identity_hash = crypto.aes_ecb(
        keys.identity_key,
        b"\x00" * 6 + random_value + address.to_bytes(2, "big"),
    )[8:]
    return b"\x01" + identity_hash + random_value


def test_stored_address_without_identity_data_remains_first_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A passive/cache gap does not discard the provisioned stable address."""
    hass = object()
    stable_device = Mock(name="stable device")
    lookup = Mock(return_value=stable_device)
    monkeypatch.setattr(resolver.bluetooth, "async_ble_device_from_address", lookup)
    monkeypatch.setattr(
        resolver.bluetooth, "async_last_service_info", Mock(return_value=None)
    )
    discovered = Mock(return_value=())
    monkeypatch.setattr(resolver.bluetooth, "async_discovered_service_info", discovered)

    candidates = resolver.async_mesh_proxy_candidates(
        hass,
        STABLE,
        net_key=NET_KEY,
        unicast_address=2,
    )

    assert candidates == (resolver.MeshProxyCandidate(STABLE, stable_device),)
    lookup.assert_called_once_with(hass, STABLE, connectable=True)
    discovered.assert_called_once_with(hass, connectable=True)


def test_recycled_stored_address_is_rejected_before_crypto_alternate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Current foreign-subnet data makes an exact random address unsafe."""
    hass = object()
    stable_device = Mock(name="recycled device")
    alternate_device = Mock(name="alternate device")
    devices = {STABLE: stable_device, ALTERNATE: alternate_device}
    lookup = Mock(side_effect=lambda _hass, address, **_kwargs: devices.get(address))
    monkeypatch.setattr(resolver.bluetooth, "async_ble_device_from_address", lookup)
    monkeypatch.setattr(
        resolver.bluetooth,
        "async_last_service_info",
        Mock(
            return_value=_info(
                STABLE,
                {MESH_PROXY_SERVICE: _network_id(b"x" * 16)},
                1.0,
            )
        ),
    )
    alternate_info = _info(
        ALTERNATE,
        {MESH_PROXY_SERVICE: _node_identity(2)},
        2.0,
    )
    monkeypatch.setattr(
        resolver.bluetooth,
        "async_discovered_service_info",
        Mock(return_value=(alternate_info,)),
    )

    candidates = resolver.async_mesh_proxy_candidates(
        hass,
        STABLE,
        net_key=NET_KEY,
        unicast_address=2,
    )

    assert candidates == (resolver.MeshProxyCandidate(ALTERNATE, alternate_device),)
    # The candidate uses HA's manager-selected BLEDevice, never info.device or
    # a source-specific scanner object retained from the advertisement.
    assert lookup.call_args_list == [call(hass, ALTERNATE, connectable=True)]


def test_alternate_hint_only_prioritizes_cryptographically_matching_adverts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A persisted transport hint is an ordering hint, never an identity."""
    hass = object()
    hinted = "22:22:22:22:22:22"
    newer = "33:33:33:33:33:33"
    untrusted = "44:44:44:44:44:44"
    devices = {
        STABLE: Mock(name="stable device"),
        hinted: Mock(name="hinted device"),
        newer: Mock(name="newer device"),
        untrusted: Mock(name="untrusted device"),
    }
    monkeypatch.setattr(
        resolver.bluetooth,
        "async_ble_device_from_address",
        Mock(side_effect=lambda _hass, address, **_kwargs: devices.get(address)),
    )
    monkeypatch.setattr(
        resolver.bluetooth, "async_last_service_info", Mock(return_value=None)
    )
    monkeypatch.setattr(
        resolver.bluetooth,
        "async_discovered_service_info",
        Mock(
            return_value=(
                _info(hinted, {MESH_PROXY_SERVICE: _network_id()}, 1.0),
                _info(newer, {MESH_PROXY_SERVICE: _node_identity(2)}, 3.0),
                _info(
                    untrusted,
                    {MESH_PROXY_SERVICE: _network_id(b"z" * 16)},
                    4.0,
                ),
            )
        ),
    )

    candidates = resolver.async_mesh_proxy_candidates(
        hass,
        STABLE,
        net_key=NET_KEY,
        unicast_address=2,
        transport_address=hinted,
    )

    assert [candidate.address for candidate in candidates] == [
        hinted,
        newer,
    ]
    assert untrusted not in {candidate.address for candidate in candidates}


def test_cached_provisioning_only_stored_address_remains_live_probe_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only fresh GATT, not cached service data, may declare a factory reset."""
    hass = object()
    stable_device = Mock()
    monkeypatch.setattr(
        resolver.bluetooth,
        "async_ble_device_from_address",
        Mock(return_value=stable_device),
    )
    monkeypatch.setattr(
        resolver.bluetooth,
        "async_last_service_info",
        Mock(
            return_value=_info(
                STABLE,
                {MESH_PROVISIONING_SERVICE: b"\x00"},
                1.0,
            )
        ),
    )
    monkeypatch.setattr(
        resolver.bluetooth,
        "async_discovered_service_info",
        Mock(return_value=()),
    )

    assert resolver.async_mesh_proxy_candidates(
        hass,
        STABLE,
        net_key=NET_KEY,
        unicast_address=2,
    ) == (resolver.MeshProxyCandidate(STABLE, stable_device),)


def test_persisted_transport_reset_remains_live_probe_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rotated fixture can open Repairs after resetting at its known route."""
    hass = object()
    transport_device = Mock()
    monkeypatch.setattr(
        resolver.bluetooth,
        "async_ble_device_from_address",
        Mock(
            side_effect=lambda _hass, address, **_kwargs: (
                transport_device if address == ALTERNATE else None
            )
        ),
    )
    monkeypatch.setattr(
        resolver.bluetooth,
        "async_last_service_info",
        Mock(
            side_effect=lambda _hass, address, **_kwargs: (
                _info(
                    ALTERNATE,
                    {MESH_PROVISIONING_SERVICE: b"\x00"},
                    2.0,
                )
                if address == ALTERNATE
                else None
            )
        ),
    )
    monkeypatch.setattr(
        resolver.bluetooth,
        "async_discovered_service_info",
        Mock(return_value=()),
    )

    assert resolver.async_mesh_proxy_candidates(
        hass,
        STABLE,
        net_key=NET_KEY,
        unicast_address=2,
        transport_address=ALTERNATE,
    ) == (resolver.MeshProxyCandidate(ALTERNATE, transport_device),)


def test_authenticated_new_route_precedes_recycled_persisted_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reset-looking old hint cannot preempt the live node's Mesh identity."""
    hass = object()
    new_address = "33:33:33:33:33:33"
    transport_device = Mock(name="recycled transport")
    new_device = Mock(name="authenticated new route")
    devices = {ALTERNATE: transport_device, new_address: new_device}
    monkeypatch.setattr(
        resolver.bluetooth,
        "async_ble_device_from_address",
        Mock(side_effect=lambda _hass, address, **_kwargs: devices.get(address)),
    )
    monkeypatch.setattr(
        resolver.bluetooth,
        "async_last_service_info",
        Mock(
            side_effect=lambda _hass, address, **_kwargs: (
                _info(
                    ALTERNATE,
                    {MESH_PROVISIONING_SERVICE: b"\x00"},
                    1.0,
                )
                if address == ALTERNATE
                else None
            )
        ),
    )
    monkeypatch.setattr(
        resolver.bluetooth,
        "async_discovered_service_info",
        Mock(
            return_value=(
                _info(new_address, {MESH_PROXY_SERVICE: _node_identity(2)}, 2.0),
            )
        ),
    )

    candidates = resolver.async_mesh_proxy_candidates(
        hass,
        STABLE,
        net_key=NET_KEY,
        unicast_address=2,
        transport_address=ALTERNATE,
    )

    assert candidates == (resolver.MeshProxyCandidate(new_address, new_device),)
