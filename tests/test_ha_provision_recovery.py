"""Home Assistant-runtime tests for crash-safe provisioning recovery."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock, call

import pytest
from bleak.exc import BleakError
from homeassistant.const import CONF_ADDRESS, CONF_NAME

from custom_components.amaran_ble import config_flow
from custom_components.amaran_ble import pending as pending_store
from custom_components.amaran_ble.amaranble.gatt import (
    MESH_PROVISIONING_SERVICE,
    MESH_PROXY_SERVICE,
    PROVISIONING_DATA_IN,
    PROVISIONING_DATA_OUT,
    PROXY_DATA_IN,
    PROXY_DATA_OUT,
)
from custom_components.amaran_ble.amaranble.network import NetworkKeys
from custom_components.amaran_ble.const import (
    CONF_APP_KEY,
    CONF_APP_PRODUCT_ID,
    CONF_DEVICE_KEY,
    CONF_NET_KEY,
    CONF_SEQUENCE_STORE_ID,
    CONF_TRANSPORT_ADDRESS,
    CONF_UNICAST_ADDRESS,
)

ADDRESS = "A4:C1:38:11:22:33"
INFO = SimpleNamespace(address=ADDRESS, name="SLCK Light")
ALTERNATE_ADDRESS = "A4:C1:38:44:55:66"
ALTERNATE_INFO = SimpleNamespace(address=ALTERNATE_ADDRESS, name="SLCK Light")


class FakeService:
    """A resolved GATT service containing selected characteristics."""

    def __init__(self, *characteristics: str) -> None:
        self.characteristics = set(characteristics)

    def get_characteristic(self, uuid: str) -> object | None:
        """Return a characteristic placeholder by UUID."""
        return object() if uuid in self.characteristics else None


class FakeServices:
    """A minimal resolved Bleak service collection."""

    def __init__(self, services: dict[str, FakeService]) -> None:
        self.services = services

    def get_service(self, uuid: str) -> FakeService | None:
        """Return a service by UUID."""
        return self.services.get(uuid)


def provisioning_service() -> FakeService:
    """Return a complete Mesh Provisioning bearer."""
    return FakeService(PROVISIONING_DATA_IN, PROVISIONING_DATA_OUT)


def proxy_service() -> FakeService:
    """Return a complete Mesh Proxy bearer."""
    return FakeService(PROXY_DATA_IN, PROXY_DATA_OUT)


@pytest.mark.asyncio
async def test_pending_enumeration_preserves_stable_keys_and_skips_bad_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Orphan recovery can enumerate records without accepting malformed values."""
    stored = {
        "fixtures": {
            ADDRESS: {"data": {CONF_NET_KEY: "01" * 16}, "committed": True},
            ALTERNATE_ADDRESS: "not a record",
            17: {"data": {CONF_NET_KEY: "02" * 16}, "committed": True},
        }
    }
    store = SimpleNamespace(async_load=AsyncMock(return_value=stored))
    monkeypatch.setattr(pending_store, "_store", Mock(return_value=store))

    records = await pending_store.async_get_pending_records(object())

    assert records == {ADDRESS: stored["fixtures"][ADDRESS]}
    assert records[ADDRESS] is not stored["fixtures"][ADDRESS]


@pytest.mark.asyncio
async def test_reprovision_rejects_a_different_detected_product_before_mutation() -> (
    None
):
    """A reset candidate cannot overwrite the configured fixture's identity."""
    info = SimpleNamespace(
        address=ADDRESS,
        name="SLCK Light",
        service_data={MESH_PROVISIONING_SERVICE: b"400U5-112233"},
    )

    with pytest.raises(
        pending_store.PendingProvisionError, match="does not match the configured"
    ):
        await config_flow.async_provision_fixture(
            object(),
            info,
            _force_reprovision=True,
            _prepared_data={CONF_APP_PRODUCT_ID: "400T5"},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("services", "expected"),
    [
        (
            {MESH_PROVISIONING_SERVICE: provisioning_service()},
            config_flow._MeshBearer.PROVISIONING,
        ),
        (
            {MESH_PROXY_SERVICE: proxy_service()},
            config_flow._MeshBearer.PROXY,
        ),
    ],
)
async def test_recovery_probe_uses_uncached_live_gatt_bearer(
    monkeypatch: pytest.MonkeyPatch,
    services: dict[str, FakeService],
    expected: config_flow._MeshBearer,
) -> None:
    """Only a complete, mutually exclusive fresh bearer is accepted."""
    hass = object()
    ble_device = object()
    disconnect = AsyncMock()
    client = SimpleNamespace(
        services=FakeServices(services),
        disconnect=disconnect,
    )
    connect = AsyncMock(return_value=client)
    lookup = Mock(return_value=ble_device)
    monkeypatch.setattr(config_flow, "establish_connection", connect)
    monkeypatch.setattr(config_flow.bluetooth, "async_ble_device_from_address", lookup)

    assert await config_flow._async_probe_mesh_bearer(hass, INFO) is expected

    lookup.assert_called_once_with(hass, ADDRESS, connectable=True)
    assert connect.await_args.args[:2] == (config_flow.BleakClient, ble_device)
    assert connect.await_args.kwargs == {
        "max_attempts": config_flow.RECOVERY_PROBE_ATTEMPTS,
        "use_services_cache": False,
    }
    disconnect.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "services",
    [
        {},
        {
            MESH_PROVISIONING_SERVICE: provisioning_service(),
            MESH_PROXY_SERVICE: proxy_service(),
        },
        {MESH_PROVISIONING_SERVICE: FakeService(PROVISIONING_DATA_IN)},
        {MESH_PROXY_SERVICE: FakeService(PROXY_DATA_OUT)},
        {
            MESH_PROVISIONING_SERVICE: FakeService(PROVISIONING_DATA_IN),
            MESH_PROXY_SERVICE: proxy_service(),
        },
    ],
)
async def test_recovery_probe_rejects_ambiguous_or_partial_bearers(
    monkeypatch: pytest.MonkeyPatch,
    services: dict[str, FakeService],
) -> None:
    """A transition or stale GATT tree can never authorize key deletion."""
    disconnect = AsyncMock()
    client = SimpleNamespace(
        services=FakeServices(services),
        disconnect=disconnect,
    )
    monkeypatch.setattr(
        config_flow.bluetooth,
        "async_ble_device_from_address",
        Mock(return_value=object()),
    )
    monkeypatch.setattr(
        config_flow, "establish_connection", AsyncMock(return_value=client)
    )

    with pytest.raises(BleakError, match="ambiguous"):
        await config_flow._async_probe_mesh_bearer(object(), INFO)

    disconnect.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_uncommitted_provisioning_bearer_reuses_prepared_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash before DATA retries without a delete-before-save window."""
    hass = object()
    record = {
        "data": {
            CONF_NET_KEY: "01" * 16,
            CONF_APP_KEY: "02" * 16,
            CONF_DEVICE_KEY: "03" * 16,
            CONF_SEQUENCE_STORE_ID: "old-sequence-store",
        },
        "committed": False,
    }
    probe = AsyncMock(return_value=config_flow._MeshBearer.PROVISIONING)
    save = AsyncMock()
    monkeypatch.setattr(config_flow, "_async_probe_mesh_bearer", probe)
    monkeypatch.setattr(config_flow, "async_save_pending", save)

    assert await config_flow._async_recover_pending(hass, INFO, record) == (
        record["data"],
        config_flow._PendingAction.REPROVISION,
    )

    probe.assert_awaited_once()
    save.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("committed", [False, None])
async def test_uncommitted_proxy_bearer_recovers_and_marks_committed(
    monkeypatch: pytest.MonkeyPatch, committed: bool | None
) -> None:
    """Crashes after DATA preserve and finalize the only usable mesh keys."""
    hass = object()
    data = {
        CONF_NET_KEY: "01" * 16,
        CONF_APP_KEY: "02" * 16,
        CONF_DEVICE_KEY: "03" * 16,
        CONF_SEQUENCE_STORE_ID: "stable-sequence-store",
    }
    record: dict[str, object] = {"data": data, "marker": "preserved"}
    if committed is not None:
        record["committed"] = committed
    probe = AsyncMock(return_value=config_flow._MeshBearer.PROXY)
    verify = AsyncMock()
    save = AsyncMock()
    monkeypatch.setattr(config_flow, "_async_probe_mesh_bearer", probe)
    monkeypatch.setattr(config_flow, "_async_verify_proxy_identity", verify)
    monkeypatch.setattr(config_flow, "async_save_pending", save)

    recovered, action = await config_flow._async_recover_pending(hass, INFO, record)

    assert recovered == data
    assert action is config_flow._PendingAction.RECOVER
    verify.assert_awaited_once()
    save.assert_awaited_once_with(
        hass,
        ADDRESS,
        {**record, "committed": True},
    )


@pytest.mark.asyncio
async def test_committed_record_recovers_offline_without_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provisioning Complete is durable proof and remains offline-recoverable."""
    data = {
        CONF_NET_KEY: "01" * 16,
        CONF_APP_KEY: "02" * 16,
        CONF_DEVICE_KEY: "03" * 16,
        CONF_SEQUENCE_STORE_ID: "stable-sequence-store",
    }
    record = {"data": data, "committed": True}
    probe = AsyncMock(side_effect=AssertionError("must not probe"))
    save = AsyncMock()
    monkeypatch.setattr(config_flow, "_async_probe_mesh_bearer", probe)
    monkeypatch.setattr(config_flow, "async_save_pending", save)

    assert await config_flow._async_recover_pending(object(), INFO, record) == (
        data,
        config_flow._PendingAction.RECOVER,
    )

    probe.assert_not_awaited()
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_rotated_proxy_recovery_requires_fresh_identity_and_keeps_stable_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cached random-address match cannot replace stable HA identity."""
    data = {
        CONF_ADDRESS: ADDRESS,
        CONF_NAME: "Stable light",
        CONF_NET_KEY: "01" * 16,
        CONF_APP_KEY: "02" * 16,
        CONF_DEVICE_KEY: "03" * 16,
        CONF_UNICAST_ADDRESS: config_flow.NODE_ADDRESS,
        CONF_SEQUENCE_STORE_ID: "stable-sequence-store",
    }
    record = {"data": data, "committed": True, "marker": "preserved"}
    probe = AsyncMock(return_value=config_flow._MeshBearer.PROXY)
    verify = AsyncMock()
    save = AsyncMock()
    monkeypatch.setattr(
        config_flow, "async_get_pending", AsyncMock(return_value=record)
    )
    monkeypatch.setattr(config_flow, "_async_probe_mesh_bearer", probe)
    monkeypatch.setattr(config_flow, "_async_verify_proxy_identity", verify)
    monkeypatch.setattr(config_flow, "async_save_pending", save)

    recovered = await config_flow.async_provision_fixture(
        object(),
        ALTERNATE_INFO,
        _recovery_address=ADDRESS,
        _require_proxy_identity=True,
    )

    assert recovered[CONF_ADDRESS] == ADDRESS
    assert recovered[CONF_TRANSPORT_ADDRESS] == ALTERNATE_ADDRESS
    probe.assert_awaited_once()
    verify.assert_awaited_once_with(
        ANY,
        ALTERNATE_INFO,
        NetworkKeys.derive(b"\x01" * 16),
        config_flow.NODE_ADDRESS,
    )
    assert save.await_args_list[-1] == call(
        ANY,
        ADDRESS,
        {
            "data": recovered,
            "committed": True,
            "marker": "preserved",
        },
    )


@pytest.mark.asyncio
async def test_rotated_recovery_rejects_stale_proxy_page_over_live_reset_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cached identity cannot attach an unrelated unprovisioned fixture."""
    record = {
        "data": {
            CONF_ADDRESS: ADDRESS,
            CONF_NET_KEY: "01" * 16,
            CONF_APP_KEY: "02" * 16,
            CONF_DEVICE_KEY: "03" * 16,
            CONF_SEQUENCE_STORE_ID: "stable-sequence-store",
        },
        "committed": True,
    }
    monkeypatch.setattr(
        config_flow, "async_get_pending", AsyncMock(return_value=record)
    )
    monkeypatch.setattr(
        config_flow,
        "_async_probe_mesh_bearer",
        AsyncMock(return_value=config_flow._MeshBearer.PROVISIONING),
    )
    verify = AsyncMock()
    save = AsyncMock()
    monkeypatch.setattr(config_flow, "_async_verify_proxy_identity", verify)
    monkeypatch.setattr(config_flow, "async_save_pending", save)

    with pytest.raises(BleakError, match="no longer exposes the Proxy"):
        await config_flow.async_provision_fixture(
            object(),
            ALTERNATE_INFO,
            _recovery_address=ADDRESS,
            _require_proxy_identity=True,
        )

    verify.assert_not_awaited()
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_ambiguous_probe_preserves_record_byte_for_byte(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Out-of-range or indeterminate recovery never mutates durable keys."""
    record = {
        "data": {
            CONF_NET_KEY: "01" * 16,
            CONF_APP_KEY: "02" * 16,
            CONF_DEVICE_KEY: "03" * 16,
        },
        "committed": False,
        "marker": {"nested": [1, 2, 3]},
    }
    original = deepcopy(record)
    monkeypatch.setattr(
        config_flow,
        "_async_probe_mesh_bearer",
        AsyncMock(side_effect=BleakError("indeterminate")),
    )
    save = AsyncMock()
    monkeypatch.setattr(config_flow, "async_save_pending", save)

    with pytest.raises(BleakError, match="indeterminate"):
        await config_flow._async_recover_pending(object(), INFO, record)

    assert record == original
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_proxy_without_matching_identity_preserves_uncommitted_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Proxy bearer owned by an unknown mesh cannot promote stale keys."""
    record = {
        "data": {
            CONF_NET_KEY: "01" * 16,
            CONF_APP_KEY: "02" * 16,
            CONF_DEVICE_KEY: "03" * 16,
            CONF_SEQUENCE_STORE_ID: "stable-sequence-store",
        },
        "committed": False,
    }
    original = deepcopy(record)
    monkeypatch.setattr(
        config_flow,
        "_async_probe_mesh_bearer",
        AsyncMock(return_value=config_flow._MeshBearer.PROXY),
    )
    monkeypatch.setattr(
        config_flow,
        "_async_verify_proxy_identity",
        AsyncMock(side_effect=BleakError("identity unavailable")),
    )
    save = AsyncMock()
    monkeypatch.setattr(config_flow, "async_save_pending", save)

    with pytest.raises(BleakError, match="identity unavailable"):
        await config_flow._async_recover_pending(object(), INFO, record)

    assert record == original
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_proxy_identity_uses_fresh_active_advertisement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery accepts only a newly observed identity matching its NetKey."""
    hass = object()
    net_key = b"\x01" * 16
    network_keys = NetworkKeys.derive(net_key)
    cancel = Mock()
    clear = Mock()
    callback_holder: dict[str, object] = {}

    def register(
        _hass: object,
        callback_fn: object,
        matcher: object,
        mode: object,
        **kwargs: object,
    ) -> Mock:
        callback_holder["callback"] = callback_fn
        callback_holder["matcher"] = matcher
        callback_holder["mode"] = mode
        callback_holder["kwargs"] = kwargs
        return cancel

    async def active_scan(_hass: object, *, duration: float) -> None:
        callback_fn = callback_holder["callback"]
        callback_fn(
            SimpleNamespace(
                service_data={MESH_PROXY_SERVICE: b"\x00" + network_keys.network_id}
            ),
            object(),
        )

    monkeypatch.setattr(config_flow.bluetooth, "async_register_callback", register)
    monkeypatch.setattr(
        config_flow.bluetooth, "async_clear_advertisement_history", clear
    )
    monkeypatch.setattr(
        config_flow.bluetooth,
        "async_request_active_scan",
        AsyncMock(side_effect=active_scan),
    )

    await config_flow._async_verify_proxy_identity(
        hass, INFO, network_keys, config_flow.NODE_ADDRESS
    )

    clear.assert_called_once_with(hass, ADDRESS)
    assert callback_holder["matcher"] == {"address": ADDRESS}
    assert callback_holder["mode"] is config_flow.BluetoothScanningMode.ACTIVE
    assert callback_holder["kwargs"] == {
        "replay": config_flow.BluetoothCallbackReplay.DISABLED
    }
    cancel.assert_called_once_with()


@pytest.mark.asyncio
async def test_before_data_recovery_runs_a_complete_new_provision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After live provisioning proof, replacement keys are saved before DATA."""
    old_record = {
        "data": {
            CONF_NET_KEY: "aa" * 16,
            CONF_APP_KEY: "bb" * 16,
            CONF_DEVICE_KEY: "cc" * 16,
            CONF_SEQUENCE_STORE_ID: "durable-sequence-store",
        },
        "committed": False,
    }
    hass = object()
    detected_info = SimpleNamespace(
        address=ADDRESS,
        name="SLCK Light",
        service_data={MESH_PROVISIONING_SERVICE: b"400T5-112233"},
    )
    client = SimpleNamespace(disconnect=AsyncMock())
    provision = AsyncMock()

    async def complete_provision(**kwargs: object) -> None:
        before_commit = kwargs["before_commit"]
        await before_commit(b"\x33" * 16, 2)

    provision.side_effect = complete_provision
    monkeypatch.setattr(
        config_flow,
        "Provisioner",
        Mock(return_value=SimpleNamespace(provision=provision)),
    )
    monkeypatch.setattr(
        config_flow,
        "_async_probe_mesh_bearer",
        AsyncMock(return_value=config_flow._MeshBearer.PROVISIONING),
    )
    monkeypatch.setattr(
        config_flow, "async_get_pending", AsyncMock(return_value=old_record)
    )
    save = AsyncMock()
    monkeypatch.setattr(config_flow, "async_save_pending", save)
    monkeypatch.setattr(
        config_flow.crypto,
        "random_bytes",
        Mock(side_effect=AssertionError("prepared keys must be reused")),
    )
    monkeypatch.setattr(
        config_flow.bluetooth,
        "async_ble_device_from_address",
        Mock(return_value=object()),
    )
    monkeypatch.setattr(
        config_flow, "establish_connection", AsyncMock(return_value=client)
    )

    recovered = await config_flow.async_provision_fixture(hass, detected_info)

    assert recovered[CONF_NET_KEY] == "aa" * 16
    assert recovered[CONF_APP_KEY] == "bb" * 16
    assert recovered[CONF_DEVICE_KEY] == "33" * 16
    assert recovered[CONF_SEQUENCE_STORE_ID] == "durable-sequence-store"
    assert recovered[CONF_APP_PRODUCT_ID] == "400T5"
    assert save.await_args_list[-2:] == [
        call(hass, ADDRESS, {"data": recovered, "committed": False}),
        call(hass, ADDRESS, {"data": recovered, "committed": True}),
    ]
    assert save.await_args_list[0].args[2]["data"][CONF_APP_PRODUCT_ID] == "400T5"
    client.disconnect.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_force_reprovision_requires_reset_and_reuses_durable_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repair reprovision atomically downgrades old completion before DATA."""
    old_data = {
        CONF_NET_KEY: "aa" * 16,
        CONF_APP_KEY: "bb" * 16,
        CONF_DEVICE_KEY: "cc" * 16,
        CONF_SEQUENCE_STORE_ID: "durable-sequence-store",
    }
    old_record = {"data": old_data, "committed": True}
    hass = object()
    client = SimpleNamespace(disconnect=AsyncMock())
    provision = AsyncMock()

    async def complete_provision(**kwargs: object) -> None:
        before_commit = kwargs["before_commit"]
        await before_commit(b"\xdd" * 16, 2)

    provision.side_effect = complete_provision
    monkeypatch.setattr(
        config_flow,
        "Provisioner",
        Mock(return_value=SimpleNamespace(provision=provision)),
    )
    probe = AsyncMock(return_value=config_flow._MeshBearer.PROVISIONING)
    monkeypatch.setattr(config_flow, "_async_probe_mesh_bearer", probe)
    monkeypatch.setattr(
        config_flow, "async_get_pending", AsyncMock(return_value=old_record)
    )
    save = AsyncMock()
    monkeypatch.setattr(config_flow, "async_save_pending", save)
    monkeypatch.setattr(
        config_flow.crypto,
        "random_bytes",
        Mock(side_effect=AssertionError("durable network must be reused")),
    )
    monkeypatch.setattr(
        config_flow.bluetooth,
        "async_ble_device_from_address",
        Mock(return_value=object()),
    )
    monkeypatch.setattr(
        config_flow, "establish_connection", AsyncMock(return_value=client)
    )

    reprovisioned = await config_flow.async_reprovision_fixture(hass, INFO)

    assert reprovisioned[CONF_NET_KEY] == old_data[CONF_NET_KEY]
    assert reprovisioned[CONF_APP_KEY] == old_data[CONF_APP_KEY]
    assert reprovisioned[CONF_DEVICE_KEY] == "dd" * 16
    assert reprovisioned[CONF_SEQUENCE_STORE_ID] == old_data[CONF_SEQUENCE_STORE_ID]
    probe.assert_awaited_once_with(hass, INFO)
    assert save.await_args_list == [
        call(hass, ADDRESS, {"data": old_data, "committed": False}),
        call(hass, ADDRESS, {"data": reprovisioned, "committed": False}),
        call(hass, ADDRESS, {"data": reprovisioned, "committed": True}),
    ]


@pytest.mark.asyncio
async def test_force_reprovision_rejects_proxy_without_mutating_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repair cannot overwrite a fixture that still exposes a Proxy bearer."""
    old_record = {
        "data": {
            CONF_NET_KEY: "aa" * 16,
            CONF_APP_KEY: "bb" * 16,
            CONF_DEVICE_KEY: "cc" * 16,
            CONF_SEQUENCE_STORE_ID: "durable-sequence-store",
        },
        "committed": True,
    }
    original = deepcopy(old_record)
    monkeypatch.setattr(
        config_flow, "async_get_pending", AsyncMock(return_value=old_record)
    )
    monkeypatch.setattr(
        config_flow,
        "_async_probe_mesh_bearer",
        AsyncMock(return_value=config_flow._MeshBearer.PROXY),
    )
    save = AsyncMock()
    connect = AsyncMock()
    monkeypatch.setattr(config_flow, "async_save_pending", save)
    monkeypatch.setattr(config_flow, "establish_connection", connect)

    with pytest.raises(BleakError, match="without an interrupted"):
        await config_flow.async_reprovision_fixture(
            object(),
            INFO,
            recovery_address=ADDRESS,
            prepared_data=old_record["data"],
        )

    assert old_record == original
    save.assert_not_awaited()
    connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_alternate_address_reprovision_uses_stable_recovery_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A changed BLE route keeps the entry's keys, sequence ID, and pending key."""
    old_data = {
        CONF_ADDRESS: ADDRESS,
        CONF_NAME: "Stable light",
        CONF_NET_KEY: "aa" * 16,
        CONF_APP_KEY: "bb" * 16,
        CONF_DEVICE_KEY: "cc" * 16,
        CONF_SEQUENCE_STORE_ID: "durable-sequence-store",
        CONF_UNICAST_ADDRESS: config_flow.NODE_ADDRESS,
    }
    old_record = {"data": old_data, "committed": True}
    hass = object()
    client = SimpleNamespace(disconnect=AsyncMock())

    async def complete_provision(**kwargs: object) -> None:
        before_commit = kwargs["before_commit"]
        await before_commit(b"\xdd" * 16, 2)

    monkeypatch.setattr(
        config_flow,
        "Provisioner",
        Mock(
            return_value=SimpleNamespace(
                provision=AsyncMock(side_effect=complete_provision)
            )
        ),
    )
    monkeypatch.setattr(
        config_flow,
        "_async_probe_mesh_bearer",
        AsyncMock(return_value=config_flow._MeshBearer.PROVISIONING),
    )
    get_pending = AsyncMock(return_value=old_record)
    monkeypatch.setattr(config_flow, "async_get_pending", get_pending)
    save = AsyncMock()
    monkeypatch.setattr(config_flow, "async_save_pending", save)
    monkeypatch.setattr(
        config_flow.crypto,
        "random_bytes",
        Mock(side_effect=AssertionError("stable network data must be reused")),
    )
    monkeypatch.setattr(
        config_flow.bluetooth,
        "async_ble_device_from_address",
        Mock(return_value=object()),
    )
    monkeypatch.setattr(
        config_flow, "establish_connection", AsyncMock(return_value=client)
    )

    reprovisioned = await config_flow.async_reprovision_fixture(
        hass,
        ALTERNATE_INFO,
        recovery_address=ADDRESS,
        prepared_data=old_data,
    )

    get_pending.assert_awaited_once_with(hass, ADDRESS)
    assert reprovisioned[CONF_ADDRESS] == ALTERNATE_ADDRESS
    assert reprovisioned[CONF_NET_KEY] == old_data[CONF_NET_KEY]
    assert reprovisioned[CONF_APP_KEY] == old_data[CONF_APP_KEY]
    assert reprovisioned[CONF_DEVICE_KEY] == "dd" * 16
    assert reprovisioned[CONF_SEQUENCE_STORE_ID] == old_data[CONF_SEQUENCE_STORE_ID]
    assert all(item.args[1] == ADDRESS for item in save.await_args_list)
    assert save.await_args_list[-1] == call(
        hass, ADDRESS, {"data": reprovisioned, "committed": True}
    )


@pytest.mark.asyncio
async def test_reprovision_seeds_missing_pending_from_existing_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing lifetime recovery data does not fork a new Mesh or sequence store."""
    old_data = {
        CONF_ADDRESS: ADDRESS,
        CONF_NAME: "Stable light",
        CONF_NET_KEY: "aa" * 16,
        CONF_APP_KEY: "bb" * 16,
        CONF_DEVICE_KEY: "cc" * 16,
        CONF_SEQUENCE_STORE_ID: "durable-sequence-store",
        CONF_UNICAST_ADDRESS: config_flow.NODE_ADDRESS,
    }
    hass = object()
    client = SimpleNamespace(disconnect=AsyncMock())

    async def complete_provision(**kwargs: object) -> None:
        before_commit = kwargs["before_commit"]
        await before_commit(b"\xee" * 16, 1)

    monkeypatch.setattr(config_flow, "async_get_pending", AsyncMock(return_value=None))
    monkeypatch.setattr(
        config_flow,
        "_async_probe_mesh_bearer",
        AsyncMock(return_value=config_flow._MeshBearer.PROVISIONING),
    )
    save = AsyncMock()
    monkeypatch.setattr(config_flow, "async_save_pending", save)
    monkeypatch.setattr(
        config_flow,
        "Provisioner",
        Mock(
            return_value=SimpleNamespace(
                provision=AsyncMock(side_effect=complete_provision)
            )
        ),
    )
    monkeypatch.setattr(
        config_flow.crypto,
        "random_bytes",
        Mock(side_effect=AssertionError("entry network data must be reused")),
    )
    monkeypatch.setattr(
        config_flow.bluetooth,
        "async_ble_device_from_address",
        Mock(return_value=object()),
    )
    monkeypatch.setattr(
        config_flow, "establish_connection", AsyncMock(return_value=client)
    )

    reprovisioned = await config_flow.async_reprovision_fixture(
        hass,
        ALTERNATE_INFO,
        recovery_address=ADDRESS,
        prepared_data=old_data,
    )

    assert reprovisioned[CONF_NET_KEY] == old_data[CONF_NET_KEY]
    assert reprovisioned[CONF_APP_KEY] == old_data[CONF_APP_KEY]
    assert reprovisioned[CONF_SEQUENCE_STORE_ID] == old_data[CONF_SEQUENCE_STORE_ID]
    assert save.await_args_list[0] == call(
        hass, ADDRESS, {"data": old_data, "committed": False}
    )
    assert all(item.args[1] == ADDRESS for item in save.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("committed", [False, True])
async def test_reprovision_retry_recovers_authenticated_proxy_after_complete(
    monkeypatch: pytest.MonkeyPatch,
    committed: bool,
) -> None:
    """A crash after DATA/Complete resumes through the alternate proxy route."""
    entry_data = {
        CONF_ADDRESS: ADDRESS,
        CONF_NAME: "Stable light",
        CONF_NET_KEY: "aa" * 16,
        CONF_APP_KEY: "bb" * 16,
        CONF_DEVICE_KEY: "cc" * 16,
        CONF_SEQUENCE_STORE_ID: "durable-sequence-store",
        CONF_UNICAST_ADDRESS: config_flow.NODE_ADDRESS,
    }
    replacement_data = {
        **entry_data,
        CONF_ADDRESS: ALTERNATE_ADDRESS,
        CONF_DEVICE_KEY: "dd" * 16,
    }
    pending = {"data": replacement_data, "committed": committed}
    hass = object()
    monkeypatch.setattr(
        config_flow, "async_get_pending", AsyncMock(return_value=pending)
    )
    monkeypatch.setattr(
        config_flow,
        "_async_probe_mesh_bearer",
        AsyncMock(return_value=config_flow._MeshBearer.PROXY),
    )
    verify = AsyncMock()
    monkeypatch.setattr(config_flow, "_async_verify_proxy_identity", verify)
    save = AsyncMock()
    monkeypatch.setattr(config_flow, "async_save_pending", save)
    connect = AsyncMock()
    monkeypatch.setattr(config_flow, "establish_connection", connect)

    recovered = await config_flow.async_reprovision_fixture(
        hass,
        ALTERNATE_INFO,
        recovery_address=ADDRESS,
        prepared_data=entry_data,
    )

    assert recovered[CONF_ADDRESS] == ALTERNATE_ADDRESS
    assert recovered[CONF_DEVICE_KEY] == replacement_data[CONF_DEVICE_KEY]
    assert recovered[CONF_SEQUENCE_STORE_ID] == "durable-sequence-store"
    verify.assert_awaited_once_with(
        hass,
        ALTERNATE_INFO,
        NetworkKeys.derive(bytes.fromhex(entry_data[CONF_NET_KEY])),
        config_flow.NODE_ADDRESS,
    )
    assert save.await_args_list[-1].args[1] == ADDRESS
    assert save.await_args_list[-1].args[2]["committed"] is True
    connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_config_flow_provision_method_delegates_to_shared_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repair callers and the config flow share one recovery implementation."""
    flow = config_flow.AmaranConfigFlow()
    hass = object()
    flow.hass = hass
    expected = {CONF_NET_KEY: "01" * 16}
    provision = AsyncMock(return_value=expected)
    monkeypatch.setattr(config_flow, "async_provision_fixture", provision)

    assert await flow._async_provision(INFO) == expected
    provision.assert_awaited_once_with(hass, INFO)
