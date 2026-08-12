"""Home Assistant-runtime tests for crash-safe device persistence helpers."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import ANY, AsyncMock, Mock

import pytest

from custom_components.amaran_ble import device
from custom_components.amaran_ble.amaranble import (
    highspeed,
    pixelfx,
    systemfx2,
    telink,
)
from custom_components.amaran_ble.amaranble.proxy import AccessMessage
from custom_components.amaran_ble.profiles import (
    ACE_25X_PROFILE,
    GENERIC_PROFILE,
    get_fixture_profile,
    get_fixture_profile_by_product_id,
)


class FakeStore:
    """Minimal async Store stand-in for merge tests."""

    def __init__(self, value: dict[str, int] | None) -> None:
        self.value = value

    async def async_load(self) -> dict[str, int] | None:
        return self.value


def make_light(
    monkeypatch: pytest.MonkeyPatch, *, profile=GENERIC_PROFILE, **kwargs: Any
) -> device.AmaranLight:
    """Build a device without touching Home Assistant's real storage manager."""
    monkeypatch.setattr(
        device,
        "_sequence_store",
        lambda _hass, _store_id: FakeStore(None),
    )
    return device.AmaranLight(
        object(),
        "sequence-store",
        "AA:BB:CC:DD:EE:FF",
        "Test light",
        net_key=b"\x01" * 16,
        app_key=b"\x02" * 16,
        device_key=b"\x03" * 16,
        unicast_address=2,
        local_address=1,
        iv_index=0,
        profile=profile,
        **kwargs,
    )


def access_message(payload: bytes) -> AccessMessage:
    """Wrap one Telink payload as an inbound fixture access message."""
    return AccessMessage(2, 1, telink.OPCODE, payload, False)


def as_report(payload: bytes, *, on: bool | None = None) -> bytes:
    """Turn a command vector into the report form emitted by a fixture."""
    report = bytearray(payload)
    report[9] &= 0x7F
    if on is not None:
        if on:
            report[1] |= 0x01
        else:
            report[1] &= 0xFE
    report[0] = sum(report[1:10]) & 0xFF
    return bytes(report)


def fan_report(mode: telink.FanMode) -> bytes:
    """Return an app-captured Ace report with Smart and Silent support."""
    report = bytearray.fromhex("7800001054fe09030109")
    report[8] = 1 if mode is telink.FanMode.SMART else 7
    report[0] = sum(report[1:10]) & 0xFF
    return bytes(report)


def light_state(
    *,
    is_hsi: bool,
    gm: float = 0,
    on: bool = True,
    intensity: int = 640,
) -> telink.LightState:
    """Return a representative fixture status."""
    return telink.LightState(
        on=on,
        is_hsi=is_hsi,
        intensity=intensity,
        kelvin=4300 if not is_hsi else 0,
        gm=gm,
        hue=120 if is_hsi else 0,
        saturation=75 if is_hsi else 0,
    )


async def wait_for_pending_steady_request(
    light: device.AmaranLight,
    expected: tuple[int | None, int | None, tuple[float, float] | None],
) -> None:
    """Yield until concurrent calls have registered their desired state."""
    for _ in range(20):
        request = light._pending_steady_request
        if (
            request is not None
            and (
                request.intensity,
                request.kelvin,
                request.hs_color,
            )
            == expected
        ):
            return
        await asyncio.sleep(0)
    raise AssertionError(f"pending steady request {expected!r} was not reached")


@pytest.mark.asyncio
async def test_sequence_store_merge_uses_highest_safe_value() -> None:
    """Recovery and rollback stores can never pull the sequence backwards."""
    stable = FakeStore({"reserved_until": 800, "sequence": 800})
    compatibility = FakeStore({"reserved_until": 1200, "sequence": 1200})

    assert await device._async_load_sequence_stores(stable, compatibility) == {
        "reserved_until": 1200,
        "sequence": 1200,
    }


@pytest.mark.asyncio
async def test_sequence_store_save_blocks_before_failed_rollback_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed compatibility write must fail before a sequence can be used."""
    writes: list[str] = []

    async def verified_save(
        _hass: Any,
        store_id: str,
        _store: FakeStore,
        _data: dict[str, int],
    ) -> None:
        writes.append(store_id)
        if store_id == "entry-id":
            raise OSError("compatibility store failed")

    monkeypatch.setattr(device, "_async_verified_sequence_save", verified_save)

    with pytest.raises(OSError, match="compatibility"):
        await device._async_save_sequence_stores(
            None,
            "stable-id",
            FakeStore(None),
            "entry-id",
            FakeStore(None),
            {"reserved_until": 1024, "sequence": 1024},
        )

    assert writes == ["stable-id", "entry-id"]


@pytest.mark.asyncio
async def test_transport_timeout_drops_stale_state_and_schedules_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed write must not leave an apparently available stale link."""
    light = make_light(monkeypatch)
    proxy = Mock()
    proxy.send_access = AsyncMock(side_effect=TimeoutError)
    proxy.stop = AsyncMock()
    client = Mock(is_connected=True)
    client.disconnect = AsyncMock()
    light._proxy = proxy
    light._client = client
    light._state = light_state(is_hsi=False)
    listener = Mock()
    light.add_listener(listener)
    schedule_reconnect = Mock()
    monkeypatch.setattr(light, "_schedule_reconnect", schedule_reconnect)

    with pytest.raises(device.AmaranConnectionError, match="failed to send"):
        await light._async_send(b"command")

    assert light._proxy is None
    assert light._client is None
    assert light.state is None
    assert not light.available
    proxy.stop.assert_awaited_once_with()
    client.disconnect.assert_awaited_once_with()
    listener.assert_called_once_with()
    schedule_reconnect.assert_called_once_with()


@pytest.mark.asyncio
async def test_async_stop_disconnects_after_background_task_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completed task exception must not prevent config-entry unload cleanup."""
    light = make_light(monkeypatch)
    proxy = Mock()
    proxy.stop = AsyncMock()
    client = Mock(is_connected=True)
    client.disconnect = AsyncMock()
    light._proxy = proxy
    light._client = client

    async def fail() -> None:
        raise RuntimeError("poll failed")

    failed_task = asyncio.create_task(fail())
    await asyncio.sleep(0)
    assert failed_task.done()
    light._poll_task = failed_task

    await light.async_stop()

    assert light._closing
    assert light._poll_task is None
    assert light._reconnect_task is None
    assert light._proxy is None
    assert light._client is None
    proxy.stop.assert_awaited_once_with()
    client.disconnect.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_async_stop_cannot_lose_race_with_inflight_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connection that resumes during unload is closed and never installed."""
    light = make_light(monkeypatch)
    connect_started = asyncio.Event()
    release_connect = asyncio.Event()
    client = Mock(is_connected=True)
    client.disconnect = AsyncMock()

    async def establish(*_args: Any, **_kwargs: Any) -> Mock:
        connect_started.set()
        await release_connect.wait()
        return client

    monkeypatch.setattr(device, "establish_connection", establish)
    monkeypatch.setattr(
        device,
        "async_mesh_proxy_candidates",
        Mock(return_value=(device.MeshProxyCandidate(light.address, Mock()),)),
    )

    connecting = asyncio.create_task(light._async_connect())
    await connect_started.wait()
    stopping = asyncio.create_task(light.async_stop())
    await asyncio.sleep(0)
    release_connect.set()

    with pytest.raises(device.AmaranConnectionError, match="shutting down"):
        await connecting
    await stopping

    assert light._client is None
    assert light._proxy is None
    client.disconnect.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_async_stop_restores_locally_started_boost_before_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A graceful reload cannot discard Boost's only output snapshot."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    original = replace(light_state(is_hsi=False, intensity=640), kelvin=4200)
    light._state = original
    light._proxy = Mock()
    light._async_send = AsyncMock()
    light._async_disconnect = AsyncMock()

    async def confirm(predicate: Any, **_kwargs: Any) -> bool:
        light._state = original
        return predicate()

    light._async_confirm_primary_state = AsyncMock(side_effect=confirm)

    await light.async_set_boost(True)
    await light.async_stop()

    payloads = [call.args[0] for call in light._async_send.await_args_list]
    assert payloads == [
        telink.boost(True, 4200, 100),
        telink.boost(False, 4200, 100),
        telink.cct(4200, 640),
        telink.onoff(True),
    ]
    assert light._closing
    assert light._boost_output_snapshot is None
    light._async_disconnect.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_connect_uses_crypto_resolved_alternate_without_changing_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A random transport address never changes stable entity/device identity."""
    light = make_light(
        monkeypatch,
        transport_address="22:22:22:22:22:22",
    )
    alternate = "11:22:33:44:55:66"
    ble_device = Mock(name="manager-selected device")
    candidate = device.MeshProxyCandidate(alternate, ble_device)
    resolve = Mock(return_value=(candidate,))
    monkeypatch.setattr(device, "async_mesh_proxy_candidates", resolve)
    client = Mock(is_connected=True)
    client.disconnect = AsyncMock()
    establish = AsyncMock(return_value=client)
    monkeypatch.setattr(device, "establish_connection", establish)
    proxy = Mock()
    proxy.start = AsyncMock()
    proxy.stop = AsyncMock()
    monkeypatch.setattr(device, "ProxyClient", Mock(return_value=proxy))
    light._async_refresh_state = AsyncMock()
    light._async_refresh_diagnostics = AsyncMock()

    await light._async_connect()

    assert light.address == "AA:BB:CC:DD:EE:FF"
    assert light.using_alternate_address
    assert light._client is client
    assert light._proxy is proxy
    resolve.assert_called_once_with(
        light.hass,
        light.address,
        net_key=b"\x01" * 16,
        unicast_address=2,
        transport_address="22:22:22:22:22:22",
    )
    assert establish.await_args.args[:3] == (
        device.BleakClient,
        ble_device,
        "Test light",
    )
    proxy.start.assert_awaited_once_with(subscribe_addresses=[2])


@pytest.mark.asyncio
async def test_not_provisioned_and_recovery_callbacks_are_episode_deduplicated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repairs callbacks fire once on loss and once after proven recovery."""
    lost = Mock()
    recovered = Mock()
    light = make_light(
        monkeypatch,
        on_not_provisioned=lost,
        on_provisioned=recovered,
    )
    for _ in range(2):
        light._notify_not_provisioned()
    lost.assert_called_once_with()
    recovered.assert_not_called()

    light._notify_provisioned()

    recovered.assert_called_once_with()


@pytest.mark.asyncio
async def test_bare_proxy_does_not_report_recovery_until_primary_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A GATT Proxy handshake cannot clear a Repair without node traffic."""
    recovered = Mock()
    light = make_light(
        monkeypatch,
        profile=ACE_25X_PROFILE,
        on_provisioned=recovered,
    )
    candidate = device.MeshProxyCandidate(light.address, Mock())
    monkeypatch.setattr(
        device, "async_mesh_proxy_candidates", Mock(return_value=(candidate,))
    )
    client = Mock(is_connected=True, disconnect=AsyncMock())
    monkeypatch.setattr(
        device, "_async_establish_candidate", AsyncMock(return_value=client)
    )
    proxy = Mock(start=AsyncMock(), stop=AsyncMock())
    monkeypatch.setattr(device, "ProxyClient", Mock(return_value=proxy))
    light._async_refresh_state = AsyncMock(return_value=False)
    light._async_refresh_diagnostics = AsyncMock()

    await light._async_connect()

    recovered.assert_not_called()
    assert light.connected
    assert not light.available

    light._on_access_message(access_message(fan_report(telink.FanMode.SMART)))
    recovered.assert_not_called()

    status = access_message(as_report(telink.cct(4300, 250), on=True))
    light._on_access_message(status)
    light._on_access_message(status)

    recovered.assert_called_once_with()
    assert light.available


@pytest.mark.asyncio
async def test_cached_provisioning_advert_with_fresh_proxy_gatt_does_not_report_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale provisioning page cannot open a Repair for a live Proxy bearer."""
    lost = Mock()
    light = make_light(monkeypatch, on_not_provisioned=lost)
    ble_device = Mock()
    monkeypatch.setattr(
        device.bluetooth,
        "async_ble_device_from_address",
        Mock(return_value=ble_device),
    )
    monkeypatch.setattr(
        device.bluetooth,
        "async_last_service_info",
        Mock(
            return_value=SimpleNamespace(
                address=light.address,
                service_data={device.MESH_PROVISIONING_SERVICE: b"\x00"},
                time=1.0,
            )
        ),
    )
    monkeypatch.setattr(
        device.bluetooth,
        "async_discovered_service_info",
        Mock(return_value=()),
    )

    proxy_service = object()
    proxy_characteristic = object()
    services = Mock()
    services.get_service.side_effect = lambda uuid: (
        proxy_service if uuid == device.MESH_PROXY_SERVICE else None
    )
    services.get_characteristic.side_effect = lambda uuid: (
        proxy_characteristic
        if uuid in {device.PROXY_DATA_IN, device.PROXY_DATA_OUT}
        else None
    )
    client = Mock(is_connected=True, services=services, disconnect=AsyncMock())
    monkeypatch.setattr(device, "establish_connection", AsyncMock(return_value=client))
    proxy = Mock(start=AsyncMock(), stop=AsyncMock())
    monkeypatch.setattr(device, "ProxyClient", Mock(return_value=proxy))
    light._async_refresh_state = AsyncMock()
    light._async_refresh_diagnostics = AsyncMock()

    await light._async_connect()

    lost.assert_not_called()
    assert light.connected


@pytest.mark.asyncio
async def test_live_provisioning_gatt_overrides_additive_proxy_advert_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fresh GATT truth reports reset despite stale matching proxy service data."""
    lost = Mock()
    light = make_light(monkeypatch, on_not_provisioned=lost)
    keys = device.network.NetworkKeys.derive(b"\x01" * 16)
    additive_info = SimpleNamespace(
        address=light.address,
        service_data={
            device.MESH_PROVISIONING_SERVICE: b"\x00",
            device.MESH_PROXY_SERVICE: b"\x00" + keys.network_id,
        },
        time=1.0,
    )
    ble_device = Mock()
    monkeypatch.setattr(
        device.bluetooth,
        "async_ble_device_from_address",
        Mock(return_value=ble_device),
    )
    monkeypatch.setattr(
        device.bluetooth,
        "async_last_service_info",
        Mock(return_value=additive_info),
    )
    monkeypatch.setattr(
        device.bluetooth,
        "async_discovered_service_info",
        Mock(return_value=()),
    )

    provisioning_service = object()
    provisioning_characteristic = object()
    services = Mock()
    services.get_service.side_effect = lambda uuid: (
        provisioning_service if uuid == device.MESH_PROVISIONING_SERVICE else None
    )
    services.get_characteristic.side_effect = lambda uuid: (
        provisioning_characteristic
        if uuid in {device.PROVISIONING_DATA_IN, device.PROVISIONING_DATA_OUT}
        else None
    )
    client = Mock(is_connected=True, services=services, disconnect=AsyncMock())
    monkeypatch.setattr(device, "establish_connection", AsyncMock(return_value=client))
    proxy_factory = Mock()
    monkeypatch.setattr(device, "ProxyClient", proxy_factory)

    with pytest.raises(device.AmaranNotProvisionedError):
        await light._async_connect()

    lost.assert_called_once_with()
    client.disconnect.assert_awaited_once_with()
    proxy_factory.assert_not_called()
    assert not light.connected


@pytest.mark.asyncio
async def test_persisted_alternate_with_live_provisioning_gatt_reports_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reset after address rotation still opens one actionable Repair."""
    alternate = "11:22:33:44:55:66"
    lost = Mock()
    light = make_light(
        monkeypatch,
        transport_address=alternate,
        on_not_provisioned=lost,
    )
    candidate = device.MeshProxyCandidate(alternate, Mock())
    resolve = Mock(return_value=(candidate,))
    monkeypatch.setattr(device, "async_mesh_proxy_candidates", resolve)

    provisioning_service = object()
    provisioning_characteristic = object()
    services = Mock()
    services.get_service.side_effect = lambda uuid: (
        provisioning_service if uuid == device.MESH_PROVISIONING_SERVICE else None
    )
    services.get_characteristic.side_effect = lambda uuid: (
        provisioning_characteristic
        if uuid in {device.PROVISIONING_DATA_IN, device.PROVISIONING_DATA_OUT}
        else None
    )
    client = Mock(is_connected=True, services=services, disconnect=AsyncMock())
    monkeypatch.setattr(
        device,
        "_async_establish_candidate",
        AsyncMock(return_value=client),
    )
    proxy_factory = Mock()
    monkeypatch.setattr(device, "ProxyClient", proxy_factory)

    with pytest.raises(device.AmaranNotProvisionedError):
        await light._async_connect()

    lost.assert_called_once_with()
    client.disconnect.assert_awaited_once_with()
    proxy_factory.assert_not_called()
    resolve.assert_called_once_with(
        light.hass,
        light.address,
        net_key=b"\x01" * 16,
        unicast_address=2,
        transport_address=alternate,
    )


@pytest.mark.asyncio
async def test_configure_stored_node_falls_back_to_crypto_resolved_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-provision recovery is not stranded on a stale stable address."""
    stable = device.MeshProxyCandidate("AA:BB:CC:DD:EE:FF", Mock())
    alternate = device.MeshProxyCandidate("11:22:33:44:55:66", Mock())
    resolve = Mock(return_value=(stable, alternate))
    monkeypatch.setattr(device, "async_mesh_proxy_candidates", resolve)
    client = Mock(disconnect=AsyncMock())
    establish = AsyncMock(side_effect=[device.BleakError("stale"), client])
    monkeypatch.setattr(device, "_async_establish_candidate", establish)
    monkeypatch.setattr(device, "_sequence_store", lambda *_args: FakeStore(None))
    configure = AsyncMock(return_value=37)
    monkeypatch.setattr(device, "async_configure_node", configure)

    result = await device.async_configure_stored_node(
        object(),
        stable.address,
        "Test light",
        net_key=b"\x01" * 16,
        app_key=b"\x02" * 16,
        device_key=b"\x03" * 16,
        unicast_address=2,
        local_address=1,
        iv_index=7,
        sequence=11,
        sequence_store_id="stable-store",
        transport_address=alternate.address,
    )

    assert result == 37
    assert [await_call.args[1] for await_call in establish.await_args_list] == [
        stable,
        alternate,
    ]
    configure.assert_awaited_once()
    client.disconnect.assert_awaited_once_with()
    resolve.assert_called_once_with(
        ANY,
        stable.address,
        net_key=b"\x01" * 16,
        unicast_address=2,
        transport_address=alternate.address,
    )


@pytest.mark.asyncio
async def test_configure_stored_node_reports_fresh_provisioning_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-provision configuration recognizes an intervening factory reset."""
    candidate = device.MeshProxyCandidate("AA:BB:CC:DD:EE:FF", Mock())
    monkeypatch.setattr(
        device, "async_mesh_proxy_candidates", Mock(return_value=(candidate,))
    )
    provisioning_service = object()
    provisioning_characteristic = object()
    services = Mock()
    services.get_service.side_effect = lambda uuid: (
        provisioning_service if uuid == device.MESH_PROVISIONING_SERVICE else None
    )
    services.get_characteristic.side_effect = lambda uuid: (
        provisioning_characteristic
        if uuid in {device.PROVISIONING_DATA_IN, device.PROVISIONING_DATA_OUT}
        else None
    )
    client = Mock(services=services, disconnect=AsyncMock())
    monkeypatch.setattr(
        device, "_async_establish_candidate", AsyncMock(return_value=client)
    )
    monkeypatch.setattr(device, "_sequence_store", lambda *_args: FakeStore(None))
    configure = AsyncMock()
    monkeypatch.setattr(device, "async_configure_node", configure)

    with pytest.raises(device.AmaranNotProvisionedError):
        await device.async_configure_stored_node(
            object(),
            candidate.address,
            "Test light",
            net_key=b"\x01" * 16,
            app_key=b"\x02" * 16,
            device_key=b"\x03" * 16,
            unicast_address=2,
            local_address=1,
            iv_index=0,
            sequence=0,
            sequence_store_id="stable-store",
        )

    configure.assert_not_awaited()
    client.disconnect.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_release_node_uses_crypto_resolved_alternate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Entry removal can safely hand back a fixture after address rotation."""
    alternate = device.MeshProxyCandidate("11:22:33:44:55:66", Mock())
    monkeypatch.setattr(
        device, "async_mesh_proxy_candidates", Mock(return_value=(alternate,))
    )
    client = Mock(is_connected=True, disconnect=AsyncMock())
    monkeypatch.setattr(
        device, "_async_establish_candidate", AsyncMock(return_value=client)
    )
    monkeypatch.setattr(device, "_sequence_store", lambda *_args: FakeStore(None))
    proxy = Mock(start=AsyncMock(), stop=AsyncMock())
    monkeypatch.setattr(device, "ProxyClient", Mock(return_value=proxy))
    config = Mock(node_reset=AsyncMock(return_value=True))
    monkeypatch.setattr(device, "ConfigClient", Mock(return_value=config))

    assert await device.async_release_node(
        object(),
        "AA:BB:CC:DD:EE:FF",
        net_key=b"\x01" * 16,
        app_key=b"\x02" * 16,
        device_key=b"\x03" * 16,
        unicast_address=2,
        local_address=1,
        iv_index=7,
        sequence_store_id="stable-store",
        transport_address=alternate.address,
    )
    proxy.start.assert_awaited_once_with(subscribe_addresses=[2])
    config.node_reset.assert_awaited_once_with()
    proxy.stop.assert_awaited_once_with()
    client.disconnect.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_poll_errors_count_as_misses_and_force_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated proxy errors cannot leave stale state available forever."""
    light = make_light(monkeypatch)
    proxy = Mock()
    light._proxy = proxy
    light._state = light_state(is_hsi=False)
    light._async_refresh_state = AsyncMock(
        side_effect=device.AmaranConnectionError("dead proxy")
    )

    async def drop(failed_proxy: Mock) -> None:
        assert failed_proxy is proxy
        light._closing = True

    light._async_drop_failed_connection = AsyncMock(side_effect=drop)
    monkeypatch.setattr(device.asyncio, "sleep", AsyncMock())

    await light._poll_loop()

    assert light._async_refresh_state.await_count == device.MAX_MISSED_POLLS
    light._async_drop_failed_connection.assert_awaited_once_with(proxy)


@pytest.mark.asyncio
async def test_effect_parameters_are_unavailable_while_effect_is_asleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auxiliary effect controls never wake a sleeping fixture unexpectedly."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    light._effect_state = telink.EffectState(
        on=False,
        effect=telink.SystemEffect.TV,
        intensity=320,
        frequency=4,
        kelvin=4300,
        variant=1,
    )
    light._async_send = AsyncMock()

    assert not light.effect_frequency_available
    assert not light.effect_color_temperature_available
    assert light.effect_variant_options == ()

    with pytest.raises(device.AmaranConnectionError, match="active effect"):
        await light.async_set_effect_frequency(5)
    with pytest.raises(device.AmaranConnectionError, match="active CCT"):
        await light.async_set_effect_kelvin(4500)
    with pytest.raises(device.AmaranConnectionError, match="active effect"):
        await light.async_set_effect_variant("cooler")

    light._async_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_not_provisioned_reconnect_backs_off_and_can_recover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale provisioning advert must not permanently strand a healthy light."""
    not_provisioned = Mock()
    light = make_light(monkeypatch, on_not_provisioned=not_provisioned)
    sleeps: list[int] = []
    attempts = 0

    async def sleep(delay: int) -> None:
        sleeps.append(delay)

    async def connect() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise device.AmaranNotProvisionedError(light.address)
        light._proxy = Mock()

    monkeypatch.setattr(device.asyncio, "sleep", sleep)
    monkeypatch.setattr(light, "_async_connect", connect)
    await light._reconnect_loop()

    assert attempts == 2
    assert sleeps == [device.RECONNECT_MIN_DELAY, device.RECONNECT_MIN_DELAY * 2]
    assert light._reconnect_delay == device.RECONNECT_MIN_DELAY * 2
    not_provisioned.assert_called_once_with()


@pytest.mark.asyncio
async def test_gm_in_hsi_mode_updates_cache_only_with_half_up_rounding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HSI has no G/M wire field, so retain the rounded preference locally."""
    light = make_light(monkeypatch)
    light._state = light_state(is_hsi=True)
    light.async_set_cct = AsyncMock()
    light._async_confirm_primary_state = AsyncMock()
    listener = Mock()
    light.add_listener(listener)

    await light.async_set_gm(2.5)
    assert light.preferred_gm == 3
    await light.async_set_gm(-2.5)
    assert light.preferred_gm == -2

    light.async_set_cct.assert_not_awaited()
    light._async_confirm_primary_state.assert_not_awaited()
    assert listener.call_count == 2


@pytest.mark.asyncio
async def test_gm_in_cct_mode_sends_then_refreshes_with_half_up_rounding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CCT mode sends the rounded tint and refreshes the fixture state cache."""
    light = make_light(monkeypatch)
    light._state = light_state(is_hsi=False, gm=-1)
    light._preferred_gm = -1
    light.async_set_cct = AsyncMock()
    light._async_confirm_primary_state = AsyncMock(return_value=True)

    await light.async_set_gm(2.5)

    light.async_set_cct.assert_awaited_once_with(4300, 640, 3)
    light._async_confirm_primary_state.assert_awaited_once()
    assert light.preferred_gm == 3


@pytest.mark.asyncio
async def test_gm_confirmation_accepts_ha_equivalent_intensity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tint-only rewrite accepts firmware quantization of held brightness."""
    light = make_light(monkeypatch)
    light._state = light_state(is_hsi=False, intensity=502)
    status_requests = 0

    async def send(payload: bytes, retries: int = 3) -> None:
        nonlocal status_requests
        del retries
        if payload == telink.status_request():
            status_requests += 1
            reported_intensity = 499 if status_requests == 1 else 501
            light._on_access_message(
                access_message(
                    as_report(telink.cct(4300, reported_intensity, 3), on=True)
                )
            )

    light._async_send = AsyncMock(side_effect=send)

    await light.async_set_gm(3)

    assert status_requests == 2
    assert light.state is not None
    assert light.state.intensity == 501
    assert light.preferred_gm == 3


@pytest.mark.asyncio
async def test_gm_v2_cct_preserves_tenths_and_sets_protocol_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cataloged G/M-v2 steady CCT uses the app's exact flag/high layout."""
    light = make_light(monkeypatch, profile=get_fixture_profile_by_product_id("05010"))
    light._state = light_state(is_hsi=False, gm=0)
    light._async_send = AsyncMock()
    light._async_confirm_primary_state = AsyncMock(return_value=True)

    await light.async_set_gm(-2.5)

    light._async_send.assert_awaited_once_with(
        telink.cct(4300, 640, -2.5, gm_flag=True)
    )
    assert light.preferred_gm == -2.5


def test_cataloged_green_magenta_range_is_applied_to_runtime_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A narrower app-declared tint range is not widened by Home Assistant."""
    light = make_light(monkeypatch, profile=get_fixture_profile_by_product_id("000J5"))

    assert light.green_magenta_min == -5
    assert light.green_magenta_max == 5


@pytest.mark.asyncio
async def test_high_speed_write_is_profile_gated_and_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The app's write-only command becomes usable state after a safe send."""
    profile = get_fixture_profile_by_product_id("40145")
    light = make_light(monkeypatch, profile=profile)
    light._async_send = AsyncMock()
    listener = Mock()
    light.add_listener(listener)

    await light.async_set_high_speed(True)

    payload = highspeed.build_high_speed(True)
    light._async_send.assert_awaited_once_with(payload)
    assert light.high_speed_state == highspeed.HighSpeedMessage(
        highspeed.HighSpeedState.ON,
        highspeed.HighSpeedOperation.APP_DEFAULT,
    )
    listener.assert_called_once_with()

    generic = make_light(monkeypatch)
    generic._async_send = AsyncMock()
    with pytest.raises(device.AmaranConnectionError, match="not enabled"):
        await generic.async_set_high_speed(True)
    generic._async_send.assert_not_awaited()


def test_high_speed_packet_updates_only_cataloged_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Either opaque operation value can carry an observed command-53 packet."""
    profile = get_fixture_profile_by_product_id("40145")
    light = make_light(monkeypatch, profile=profile)
    listener = Mock()
    light.add_listener(listener)
    packet = highspeed.build_high_speed(
        True, operation=highspeed.HighSpeedOperation.OPAQUE_1
    )

    light._on_access_message(access_message(packet))

    assert light.high_speed_state == highspeed.HighSpeedMessage(
        highspeed.HighSpeedState.ON,
        highspeed.HighSpeedOperation.OPAQUE_1,
    )
    assert light._high_speed_received.is_set()
    assert not light._state_received.is_set()
    listener.assert_called_once_with()


def test_typed_reports_update_only_their_own_cache_and_waiter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Optional report pages cannot falsely satisfy a primary state request."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    light._proxy = Mock()
    listener = Mock()
    light.add_listener(listener)

    version = bytes.fromhex("a3b7cca7f40c6e62a900")
    version2 = bytes.fromhex("4d0000d08cc2cb6ad525")
    power = bytes.fromhex("f80000779ba43800000a")
    effect = as_report(
        telink.effect(
            telink.SystemEffect.LIGHTNING,
            intensity=320,
            frequency=11,
            speed=7,
            trigger=2,
            kelvin=4300,
        )
    )

    light._on_access_message(access_message(version))
    assert light.version_state is not None
    assert light._version_received.is_set()
    assert not light._state_received.is_set()

    light._on_access_message(access_message(version2))
    assert light.version2_state is not None
    assert light.version2_state.active_system_effect_groups == ("A", "C", "E", "G")
    assert light._version2_received.is_set()
    assert not light._state_received.is_set()

    light._on_access_message(access_message(power))
    assert light.power_state is not None
    assert light.power_state.runtime_minutes == 3000
    assert light._power_received.is_set()
    assert not light._state_received.is_set()

    light._on_access_message(access_message(effect))
    assert light.effect_state == telink.decode_effect(effect)
    assert light._state_received.is_set()
    assert light.available
    assert listener.call_count == 4


def test_generic_profile_ignores_every_ace_only_report_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unsolicited vendor pages never enable model-specific behavior."""
    light = make_light(monkeypatch)
    listener = Mock()
    light.add_listener(listener)
    payloads = [
        as_report(telink.effect(telink.SystemEffect.TV)),
        as_report(telink.boost(True, 4300)),
        bytes.fromhex("7800001054fe09030109"),
        bytes.fromhex("f80000779ba43800000a"),
        bytes.fromhex("a3b7cca7f40c6e62a900"),
        bytes.fromhex("4d0000d08cc2cb6ad525"),
        highspeed.build_high_speed(
            True, operation=highspeed.HighSpeedOperation.OPAQUE_1
        ),
    ]

    for payload in payloads:
        light._on_access_message(access_message(payload))

    assert light.effect_state is None
    assert light.boost_state is None
    assert light.fan_state is None
    assert light.power_state is None
    assert light.version_state is None
    assert light.version2_state is None
    assert light.high_speed_state is None
    assert not light._state_received.is_set()
    assert not light._boost_received.is_set()
    assert not light._fan_received.is_set()
    assert not light._power_received.is_set()
    assert not light._version_received.is_set()
    assert not light._version2_received.is_set()
    assert not light._high_speed_received.is_set()
    listener.assert_not_called()


@pytest.mark.asyncio
async def test_diagnostic_queries_are_profile_gated_and_version_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Protocol version must precede power decoding; Generic sends no pages."""
    ace = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    requests: list[bytes] = []

    async def refresh(payload: bytes, event: asyncio.Event) -> bool:
        del event
        requests.append(payload)
        return False

    ace._async_refresh_optional = AsyncMock(side_effect=refresh)
    await ace._async_refresh_diagnostics(include_version=True)

    assert requests == [
        telink.version_request(),
        telink.version2_request(),
        telink.power_request(),
        telink.fan_request(),
    ]

    generic = make_light(monkeypatch)
    generic._async_refresh_optional = AsyncMock()
    await generic._async_refresh_diagnostics(include_version=True)
    generic._async_refresh_optional.assert_not_awaited()


@pytest.mark.asyncio
async def test_effect_rate_rebuild_preserves_unexposed_packet_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Changing rate must retain the fixture-reported speed and trigger fields."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    light._effect_state = telink.EffectState(
        on=True,
        effect=telink.SystemEffect.LIGHTNING,
        intensity=320,
        frequency=4,
        speed=7,
        trigger=2,
        kelvin=4300,
        gm=100,
    )
    light._async_send = AsyncMock()
    light._async_confirm_primary_state = AsyncMock(return_value=True)

    await light.async_set_effect_frequency(11)

    payload = light._async_send.await_args.args[0]
    assert telink.decode_effect(payload) == telink.EffectState(
        on=True,
        effect=telink.SystemEffect.LIGHTNING,
        intensity=320,
        frequency=11,
        speed=7,
        trigger=2,
        kelvin=4300,
        gm=100,
    )
    light._async_confirm_primary_state.assert_awaited_once()


@pytest.mark.asyncio
async def test_effect_cct_and_variant_preserve_the_complete_packet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """App-visible colour controls retain opaque speed and trigger fields."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    light._effect_state = telink.EffectState(
        on=True,
        effect=telink.SystemEffect.LIGHTNING,
        intensity=320,
        frequency=4,
        speed=7,
        trigger=2,
        kelvin=4300,
    )
    light._async_send = AsyncMock()
    light._async_confirm_primary_state = AsyncMock(return_value=True)

    await light.async_set_effect_kelvin(4325)
    cct_state = telink.decode_effect(light._async_send.await_args_list[0].args[0])
    assert cct_state == telink.EffectState(
        on=True,
        effect=telink.SystemEffect.LIGHTNING,
        intensity=320,
        frequency=4,
        speed=7,
        trigger=2,
        kelvin=4350,
        gm=100,
    )

    light._effect_state = telink.EffectState(
        on=True,
        effect=telink.SystemEffect.TV,
        intensity=320,
        frequency=4,
        variant=0,
    )
    await light.async_set_effect_variant("cooler")
    variant_state = telink.decode_effect(light._async_send.await_args_list[1].args[0])
    assert variant_state == telink.EffectState(
        on=True,
        effect=telink.SystemEffect.TV,
        intensity=320,
        frequency=4,
        variant=2,
    )


@pytest.mark.asyncio
async def test_effect_gm_uses_app_raw_scale_and_preserves_complete_packet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The normalized slider maps to raw app tint without changing FX fields."""
    light = make_light(monkeypatch, profile=get_fixture_profile("ace_25c"))
    light._effect_state = telink.EffectState(
        on=True,
        effect=telink.SystemEffect.LIGHTNING,
        intensity=320,
        frequency=4,
        speed=7,
        trigger=2,
        kelvin=4300,
        gm=100,
    )
    light._async_send = AsyncMock()
    light._async_confirm_primary_state = AsyncMock(return_value=True)

    await light.async_set_effect_gm(-2.5)

    state = telink.decode_effect(light._async_send.await_args.args[0])
    assert state == telink.EffectState(
        on=True,
        effect=telink.SystemEffect.LIGHTNING,
        intensity=320,
        frequency=4,
        speed=7,
        trigger=2,
        kelvin=4300,
        gm=80,
    )


@pytest.mark.asyncio
async def test_effect_gm_v2_preserves_and_writes_fine_tint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cataloged G/M-v2 fixtures keep exact raw tint across effect rewrites."""
    light = make_light(monkeypatch, profile=get_fixture_profile_by_product_id("05010"))
    light._effect_state = telink.decode_effect(
        telink.effect(
            telink.SystemEffect.LIGHTNING,
            intensity=320,
            frequency=4,
            speed=7,
            trigger=2,
            kelvin=4300,
            gm=135,
            gm_flag=True,
        )
    )
    assert light._effect_state is not None
    light._async_send = AsyncMock()
    light._async_confirm_primary_state = AsyncMock(return_value=True)

    await light.async_set_effect_frequency(6)
    preserved = telink.decode_effect(light._async_send.await_args.args[0])
    assert preserved is not None
    assert preserved.gm == 135
    assert preserved.gm_flag is True

    light._async_send.reset_mock()
    await light.async_set_effect_gm(-2.5)
    changed = telink.decode_effect(light._async_send.await_args.args[0])
    assert changed is not None
    assert changed.gm == 75
    assert changed.gm_flag is True


@pytest.mark.asyncio
async def test_new_legacy_effect_enables_cataloged_gm_v2_layout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """New cmd7 selections use the exact G/M layout advertised by the model."""
    light = make_light(monkeypatch, profile=get_fixture_profile_by_product_id("05010"))
    light._state = light_state(is_hsi=False, on=True)
    light._async_send = AsyncMock()
    light._async_confirm_primary_state = AsyncMock(return_value=True)

    await light.async_apply_effect(telink.SystemEffect.PAPARAZZI)

    state = telink.decode_effect(light._async_send.await_args.args[0])
    assert state is not None
    assert state.effect is telink.SystemEffect.PAPARAZZI
    assert state.gm_flag is True


@pytest.mark.asyncio
async def test_full_color_profile_selects_and_preserves_legacy_hsi_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full-colour models use the app-default HSI branch for classic effects."""
    light = make_light(monkeypatch, profile=get_fixture_profile("ace_25c"))
    light._state = light_state(is_hsi=True, on=True)
    light._async_send = AsyncMock()
    light._async_confirm_primary_state = AsyncMock(return_value=True)

    await light.async_apply_effect(telink.SystemEffect.FAULTY_BULB)

    selected = telink.decode_effect(light._async_send.await_args.args[0])
    assert selected is not None
    assert selected.mode == 1
    assert selected.hue == 120
    assert selected.saturation == 75
    assert selected.kelvin is None
    assert selected.gm is None

    light._effect_state = selected
    light._async_send.reset_mock()
    await light.async_set_effect_hue(321)
    updated = telink.decode_effect(light._async_send.await_args.args[0])
    assert updated is not None
    assert updated.mode == 1
    assert updated.hue == 321
    assert updated.saturation == 75


@pytest.mark.asyncio
async def test_full_color_profile_defaults_legacy_effect_to_steady_cct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full-color fixture's CCT look must not be changed to HSI by FX entry."""
    light = make_light(monkeypatch, profile=get_fixture_profile("ace_25c"))
    light._state = light_state(is_hsi=False, gm=-2)
    light._preferred_gm = -2
    light._async_send = AsyncMock()
    light._async_confirm_primary_state = AsyncMock(return_value=True)

    await light.async_apply_effect(telink.SystemEffect.FAULTY_BULB)

    selected = telink.decode_effect(light._async_send.await_args.args[0])
    assert selected is not None
    assert selected.mode == 0
    assert selected.kelvin == 4300
    assert selected.gm == 80
    assert selected.hue is None
    assert selected.saturation is None


@pytest.mark.asyncio
async def test_legacy_effect_color_mode_switch_preserves_and_confirms_exact_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Switching CCT/HSI keeps common fields and requires the exact fresh report."""
    light = make_light(monkeypatch, profile=get_fixture_profile("ace_25c"))
    light._state = light_state(is_hsi=True)
    light._effect_state = telink.EffectState(
        on=True,
        effect=telink.SystemEffect.WELDING,
        intensity=320,
        frequency=4,
        speed=0,
        trigger=2,
        mode=0,
        kelvin=4300,
        gm=100,
    )
    light._async_send = AsyncMock()
    light._async_confirm_primary_state = AsyncMock(return_value=True)

    assert light.effect_color_mode_options == ("cct", "hsi")
    assert light.effect_color_mode == "cct"
    await light.async_set_effect_color_mode("hsi")

    switched = telink.decode_effect(light._async_send.await_args.args[0])
    assert switched == telink.EffectState(
        on=True,
        effect=telink.SystemEffect.WELDING,
        intensity=320,
        frequency=4,
        speed=0,
        trigger=2,
        mode=1,
        hue=120,
        saturation=75,
    )
    predicate = light._async_confirm_primary_state.await_args.args[0]
    light._effect_state = replace(switched, frequency=5)
    assert not predicate()
    light._effect_state = switched
    assert predicate()

    light._state = light_state(is_hsi=False, gm=-2)
    light._preferred_gm = -2
    light._async_send.reset_mock()
    await light.async_set_effect_color_mode("cct")
    restored = telink.decode_effect(light._async_send.await_args.args[0])
    assert restored == telink.EffectState(
        on=True,
        effect=telink.SystemEffect.WELDING,
        intensity=320,
        frequency=4,
        speed=0,
        trigger=2,
        mode=0,
        kelvin=4300,
        gm=80,
    )


@pytest.mark.asyncio
async def test_welding_hue_and_saturation_preserve_complete_hsi_packet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Editing Welding HSI parameters must not switch it into CCT mode."""
    light = make_light(monkeypatch, profile=get_fixture_profile("ace_25c"))
    light._effect_state = telink.EffectState(
        on=True,
        effect=telink.SystemEffect.WELDING,
        intensity=320,
        frequency=4,
        speed=18,
        trigger=2,
        mode=1,
        hue=120,
        saturation=75,
    )
    light._async_send = AsyncMock()
    light._async_confirm_primary_state = AsyncMock(return_value=True)

    await light.async_set_effect_hue(121.5)
    hue_state = telink.decode_effect(light._async_send.await_args_list[0].args[0])
    assert hue_state == telink.EffectState(
        on=True,
        effect=telink.SystemEffect.WELDING,
        intensity=320,
        frequency=4,
        speed=18,
        trigger=2,
        mode=1,
        hue=122,
        saturation=75,
    )

    light._effect_state = hue_state
    await light.async_set_effect_saturation(76.5)
    saturation_state = telink.decode_effect(
        light._async_send.await_args_list[1].args[0]
    )
    assert saturation_state == telink.EffectState(
        on=True,
        effect=telink.SystemEffect.WELDING,
        intensity=320,
        frequency=4,
        speed=18,
        trigger=2,
        mode=1,
        hue=122,
        saturation=77,
    )


@pytest.mark.asyncio
async def test_effect_parameter_confirmation_rejects_changed_preserved_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A target rate with a wrong retained field cannot confirm the write."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    light._effect_state = telink.EffectState(
        on=True,
        effect=telink.SystemEffect.LIGHTNING,
        intensity=320,
        frequency=4,
        speed=7,
        trigger=2,
        kelvin=4300,
    )
    status_requests = 0

    async def send(payload: bytes, retries: int = 3) -> None:
        nonlocal status_requests
        del retries
        if payload == telink.status_request():
            status_requests += 1
            wrong = telink.effect(
                telink.SystemEffect.LIGHTNING,
                intensity=320,
                frequency=6,
                speed=1,
                trigger=2,
                kelvin=4300,
            )
            light._on_access_message(access_message(as_report(wrong)))

    light._async_send = AsyncMock(side_effect=send)

    with pytest.raises(device.AmaranConnectionError, match="effect rate"):
        await light.async_set_effect_frequency(6)

    assert status_requests == 3


@pytest.mark.asyncio
async def test_effect_parameter_confirmation_accepts_ha_equivalent_intensity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cmd7 rewrite accepts quantization while preserving every other field."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    light._effect_state = telink.EffectState(
        on=True,
        effect=telink.SystemEffect.LIGHTNING,
        intensity=502,
        frequency=4,
        speed=7,
        trigger=2,
        kelvin=4300,
    )
    status_requests = 0

    async def send(payload: bytes, retries: int = 3) -> None:
        nonlocal status_requests
        del retries
        if payload == telink.status_request():
            status_requests += 1
            report = telink.effect(
                telink.SystemEffect.LIGHTNING,
                intensity=501,
                frequency=6,
                speed=7,
                trigger=2,
                kelvin=4300,
            )
            light._on_access_message(access_message(as_report(report)))

    light._async_send = AsyncMock(side_effect=send)

    await light.async_set_effect_frequency(6)

    assert status_requests == 1
    assert light.effect_state is not None
    assert light.effect_state.intensity == 501


@pytest.mark.asyncio
async def test_effect_switch_keeps_active_intensity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Changing FX without brightness keeps the visible effect intensity."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    light._state = light_state(is_hsi=False, intensity=640)
    light._effect_state = telink.EffectState(
        on=True,
        effect=telink.SystemEffect.TV,
        intensity=210,
        frequency=4,
    )
    light._async_send = AsyncMock()
    light._async_confirm_primary_state = AsyncMock(return_value=True)

    await light.async_apply_effect(telink.SystemEffect.FIRE)

    payload = light._async_send.await_args_list[0].args[0]
    state = telink.decode_effect(payload)
    assert state is not None
    assert state.effect is telink.SystemEffect.FIRE
    assert state.intensity == 210
    assert light._async_send.await_count == 1


@pytest.mark.asyncio
async def test_legacy_effect_keeps_cross_generation_intensity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Switching command families preserves the currently visible intensity."""
    light = make_light(monkeypatch, profile=get_fixture_profile_by_product_id("000G5"))
    light._state = light_state(is_hsi=False, intensity=100)
    light._effect2_state = systemfx2.decode_effect2(
        systemfx2.effect2(
            systemfx2.SystemEffect2.LIGHTNING_II,
            intensity=700,
            frequency=5,
            speed=5,
            mode=0,
            kelvin=4300,
            gm=100,
        )[0]
    )
    light._async_send = AsyncMock()
    light._async_confirm_primary_state = AsyncMock(return_value=True)

    await light.async_apply_effect(telink.SystemEffect.FIRE)

    state = telink.decode_effect(light._async_send.await_args.args[0])
    assert state is not None
    assert state.effect is telink.SystemEffect.FIRE
    assert state.intensity == 700
    light._async_send.assert_awaited_once()


@pytest.mark.asyncio
async def test_new_effect_accepts_firmware_normalized_opaque_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new cmd7 effect accepts normalized intensity and opaque defaults."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    light._state = light_state(is_hsi=False, intensity=502)

    async def send(payload: bytes, retries: int = 3) -> None:
        del retries
        if payload != telink.status_request():
            return
        normalized = telink.effect(
            telink.SystemEffect.PULSING,
            intensity=501,
            frequency=5,
            speed=1,
            trigger=2,
            kelvin=4300,
        )
        light._on_access_message(access_message(as_report(normalized)))

    light._async_send = AsyncMock(side_effect=send)

    await light.async_apply_effect(telink.SystemEffect.PULSING, intensity=502)

    assert light.effect_state is not None
    assert light.effect_state.effect is telink.SystemEffect.PULSING
    assert light.effect_state.intensity == 501
    assert light.effect_state.speed == 1


@pytest.mark.asyncio
async def test_active_effect_brightness_still_requires_full_preservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Editing the active FX may not silently reset a field already reported."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    light._effect_state = telink.EffectState(
        on=True,
        effect=telink.SystemEffect.PULSING,
        intensity=10,
        frequency=4,
        speed=7,
        trigger=2,
        kelvin=4300,
    )

    async def send(payload: bytes, retries: int = 3) -> None:
        del retries
        if payload != telink.status_request():
            return
        changed = telink.effect(
            telink.SystemEffect.PULSING,
            intensity=20,
            frequency=4,
            speed=1,
            trigger=2,
            kelvin=4300,
        )
        light._on_access_message(access_message(as_report(changed)))

    light._async_send = AsyncMock(side_effect=send)

    with pytest.raises(device.AmaranConnectionError, match="did not confirm effect"):
        await light.async_apply_effect(telink.SystemEffect.PULSING, intensity=20)


@pytest.mark.asyncio
async def test_effect_off_turns_on_steady_mode_even_without_active_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HA turn_on(effect=off) cannot become a no-op on a sleeping light."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    light._state = light_state(is_hsi=False, on=False)
    light._async_send = AsyncMock()
    light._async_confirm_primary_state = AsyncMock(return_value=True)

    await light.async_apply_effect("off")

    assert [call.args[0] for call in light._async_send.await_args_list] == [
        telink.cct(4300, 640),
        telink.onoff(True),
    ]


@pytest.mark.asyncio
async def test_sleeping_effect_exit_restores_parameters_before_power(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Leaving a sleeping FX never wakes at the old effect or stale look."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    light._state = light_state(is_hsi=False, gm=1, on=False)
    light._preferred_gm = 1
    light._effect_state = telink.EffectState(
        on=False,
        effect=telink.SystemEffect.TV,
        intensity=200,
        frequency=5,
    )
    light._async_send = AsyncMock()
    light._async_confirm_primary_state = AsyncMock(return_value=True)

    await light.async_apply_effect("off", intensity=100)

    assert [call.args[0] for call in light._async_send.await_args_list] == [
        telink.cct(4300, 100, 1),
        telink.onoff(True),
    ]


@pytest.mark.parametrize(
    ("target_intensity", "reported_intensity", "succeeds"),
    [(502, 501, True), (500, 499, False)],
)
@pytest.mark.asyncio
async def test_effect_exit_confirms_implicit_target_in_ha_brightness_domain(
    monkeypatch: pytest.MonkeyPatch,
    target_intensity: int,
    reported_intensity: int,
    succeeds: bool,
) -> None:
    """Effect exit accepts quantization but rejects another published brightness."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    light._state = light_state(is_hsi=False, intensity=target_intensity)
    light._effect_state = telink.EffectState(
        on=True,
        effect=telink.SystemEffect.TV,
        intensity=200,
        frequency=5,
    )
    status_requests = 0

    async def send(payload: bytes, retries: int = 3) -> None:
        nonlocal status_requests
        del retries
        if payload == telink.status_request():
            status_requests += 1
            light._on_access_message(
                access_message(as_report(telink.cct(4300, reported_intensity), on=True))
            )

    light._async_send = AsyncMock(side_effect=send)

    if succeeds:
        await light.async_apply_effect("off")
        assert status_requests == 1
    else:
        with pytest.raises(device.AmaranConnectionError, match="leaving its effect"):
            await light.async_apply_effect("off")
        assert status_requests == 3


@pytest.mark.asyncio
async def test_steady_parameter_exits_sleeping_effect_and_wakes_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CCT requested during sleeping FX always ends with an ON command."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    light._state = light_state(is_hsi=False, on=True)
    light._effect_state = telink.EffectState(
        on=False,
        effect=telink.SystemEffect.TV,
        intensity=200,
        frequency=5,
    )
    light._async_send = AsyncMock()
    light._async_confirm_primary_state = AsyncMock(return_value=True)

    await light.async_apply_turn_on(
        intensity=100,
        brightness_changed=True,
        kelvin=3200,
    )

    assert [call.args[0] for call in light._async_send.await_args_list] == [
        telink.cct(3200, 100),
        telink.onoff(True),
    ]


@pytest.mark.asyncio
async def test_steady_turn_on_accepts_one_step_firmware_intensity_quantization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A one-tenth-percent Ace normalization still confirms the HA request."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    light._state = light_state(is_hsi=False, on=True, intensity=100)
    status_requests = 0

    async def send(payload: bytes, retries: int = 3) -> None:
        nonlocal status_requests
        del retries
        if payload == telink.status_request():
            status_requests += 1
            light._on_access_message(
                access_message(as_report(telink.cct(4500, 501), on=True))
            )

    light._async_send = AsyncMock(side_effect=send)

    await light.async_apply_turn_on(
        intensity=502,
        brightness_changed=True,
        kelvin=4500,
    )

    assert status_requests == 1
    assert light.state is not None and light.state.intensity == 501


@pytest.mark.asyncio
async def test_brightness_only_write_accepts_same_ha_brightness_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Command 15 uses the same user-visible confirmation contract."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    light._state = light_state(is_hsi=False, on=True, intensity=100)

    async def send(payload: bytes, retries: int = 3) -> None:
        del retries
        if payload == telink.status_request():
            light._on_access_message(
                access_message(as_report(telink.cct(4500, 501), on=True))
            )

    light._async_send = AsyncMock(side_effect=send)

    await light.async_apply_turn_on(
        intensity=502,
        brightness_changed=True,
    )

    assert [call.args[0] for call in light._async_send.await_args_list] == [
        telink.brightness(502),
        telink.status_request(),
    ]
    assert light.state is not None and light.state.intensity == 501


@pytest.mark.asyncio
async def test_rapid_brightness_updates_skip_intermediate_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the newest slider value is sent after the in-flight transaction."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    light._state = light_state(is_hsi=False, on=True, intensity=100)
    first_write_started = asyncio.Event()
    release_first_write = asyncio.Event()
    payloads: list[bytes] = []

    async def send(payload: bytes, retries: int = 3) -> None:
        del retries
        payloads.append(payload)
        if payload == telink.brightness(200):
            first_write_started.set()
            await release_first_write.wait()
        elif payload == telink.status_request():
            light._on_access_message(
                access_message(as_report(telink.cct(4300, 400), on=True))
            )

    light._async_send = AsyncMock(side_effect=send)
    first = asyncio.create_task(
        light.async_apply_turn_on(intensity=200, brightness_changed=True)
    )
    await first_write_started.wait()
    middle = asyncio.create_task(
        light.async_apply_turn_on(intensity=300, brightness_changed=True)
    )
    latest = asyncio.create_task(
        light.async_apply_turn_on(intensity=400, brightness_changed=True)
    )
    await wait_for_pending_steady_request(light, (400, None, None))
    release_first_write.set()

    await asyncio.wait_for(asyncio.gather(first, middle, latest), 1)

    assert payloads == [
        telink.brightness(200),
        telink.brightness(400),
        telink.status_request(),
    ]
    assert light.state is not None and light.state.intensity == 400
    assert light._pending_steady_request is None


@pytest.mark.asyncio
async def test_new_slider_value_wakes_obsolete_confirmation_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A newer value supersedes the exact unanswered read seen in live HA."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    light._state = light_state(is_hsi=False, on=True, intensity=100)
    first_status_sent = asyncio.Event()
    payloads: list[bytes] = []
    status_requests = 0

    async def send(payload: bytes, retries: int = 3) -> None:
        nonlocal status_requests
        del retries
        payloads.append(payload)
        if payload != telink.status_request():
            return
        status_requests += 1
        if status_requests == 1:
            first_status_sent.set()
            return
        light._on_access_message(
            access_message(as_report(telink.cct(4300, 400), on=True))
        )

    light._async_send = AsyncMock(side_effect=send)
    obsolete = asyncio.create_task(
        light.async_apply_turn_on(intensity=200, brightness_changed=True)
    )
    await first_status_sent.wait()
    # Let the first operation reach its 0.7-second report wait before a newer
    # frontend slider value arrives.
    await asyncio.sleep(0)
    assert light._operation_lock.locked()
    assert not obsolete.done()

    latest = asyncio.create_task(
        light.async_apply_turn_on(intensity=400, brightness_changed=True)
    )
    await wait_for_pending_steady_request(light, (400, None, None))

    await asyncio.wait_for(obsolete, 1)
    await asyncio.wait_for(latest, 1)

    assert payloads == [
        telink.brightness(200),
        telink.status_request(),
        telink.brightness(400),
        telink.status_request(),
    ]
    assert status_requests == 2
    assert light.state is not None and light.state.intensity == 400


@pytest.mark.asyncio
async def test_cancelled_queued_slider_request_does_not_leak_partial_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation before the device lock cannot contaminate a later call."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    light._state = light_state(is_hsi=False, on=True, intensity=100)
    first_write_started = asyncio.Event()
    release_first_write = asyncio.Event()
    payloads: list[bytes] = []

    async def send(payload: bytes, retries: int = 3) -> None:
        del retries
        payloads.append(payload)
        if payload == telink.brightness(200):
            first_write_started.set()
            await release_first_write.wait()
        elif payload == telink.status_request():
            light._on_access_message(
                access_message(as_report(telink.cct(4500, 100), on=True))
            )

    light._async_send = AsyncMock(side_effect=send)
    first = asyncio.create_task(
        light.async_apply_turn_on(intensity=200, brightness_changed=True)
    )
    await first_write_started.wait()
    cancelled = asyncio.create_task(
        light.async_apply_turn_on(intensity=300, brightness_changed=True)
    )
    await wait_for_pending_steady_request(light, (300, None, None))
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    assert light._pending_steady_request is None

    release_first_write.set()
    await asyncio.wait_for(first, 1)
    latest = asyncio.create_task(
        light.async_apply_turn_on(
            intensity=999,
            brightness_changed=False,
            kelvin=4500,
        )
    )
    await asyncio.wait_for(latest, 1)

    assert payloads == [
        telink.brightness(200),
        telink.cct(4500, 100),
        telink.status_request(),
    ]
    assert telink.brightness(300) not in payloads
    assert light._pending_steady_request is None


@pytest.mark.asyncio
async def test_rapid_effect_brightness_updates_only_confirm_latest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Latest-wins also covers brightness while a legacy effect is active."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    light._effect_state = telink.EffectState(
        on=True,
        effect=telink.SystemEffect.PULSING,
        intensity=100,
        frequency=4,
        speed=7,
        trigger=2,
        kelvin=4300,
    )
    first_payload = light._effect_payload(light._effect_state, intensity=200)
    middle_payload = light._effect_payload(light._effect_state, intensity=300)
    final_payload = light._effect_payload(light._effect_state, intensity=400)
    first_write_started = asyncio.Event()
    release_first_write = asyncio.Event()
    payloads: list[bytes] = []

    async def send(payload: bytes, retries: int = 3) -> None:
        del retries
        payloads.append(payload)
        if payload == first_payload:
            first_write_started.set()
            await release_first_write.wait()
        elif payload == telink.status_request():
            light._on_access_message(access_message(as_report(final_payload)))

    light._async_send = AsyncMock(side_effect=send)
    first = asyncio.create_task(
        light.async_apply_turn_on(intensity=200, brightness_changed=True)
    )
    await first_write_started.wait()
    middle = asyncio.create_task(
        light.async_apply_turn_on(intensity=300, brightness_changed=True)
    )
    latest = asyncio.create_task(
        light.async_apply_turn_on(intensity=400, brightness_changed=True)
    )
    await wait_for_pending_steady_request(light, (400, None, None))
    release_first_write.set()

    await asyncio.wait_for(asyncio.gather(first, middle, latest), 1)

    assert payloads == [first_payload, final_payload, telink.status_request()]
    assert middle_payload not in payloads
    assert light.effect_state is not None
    assert light.effect_state.intensity == 400


@pytest.mark.asyncio
async def test_rapid_effect2_brightness_completes_packets_then_sends_latest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An obsolete multi-packet effect2 write finishes before latest wins."""
    profile = get_fixture_profile_by_product_id("000G5")
    light = make_light(monkeypatch, profile=profile)
    for payload in systemfx2.effect2(
        systemfx2.SystemEffect2.LIGHTNING_II,
        intensity=100,
        frequency=5,
        speed=7,
        mode=1,
        hue=120,
        saturation=75,
        center_kelvin=5600,
    ):
        light._on_access_message(access_message(as_report(payload)))
    assert light.effect2_state is not None
    first_payloads = light._effect2_payloads(light.effect2_state, intensity=200)
    latest_payloads = light._effect2_payloads(light.effect2_state, intensity=400)
    first_write_started = asyncio.Event()
    release_first_write = asyncio.Event()
    payloads: list[bytes] = []

    async def send(payload: bytes, retries: int = 3) -> None:
        del retries
        payloads.append(payload)
        if payload == first_payloads[0]:
            first_write_started.set()
            await release_first_write.wait()
        elif payload == telink.status_request():
            for report in latest_payloads:
                light._on_access_message(access_message(as_report(report)))

    light._async_send = AsyncMock(side_effect=send)
    obsolete = asyncio.create_task(
        light.async_apply_turn_on(intensity=200, brightness_changed=True)
    )
    await first_write_started.wait()
    latest = asyncio.create_task(
        light.async_apply_turn_on(intensity=400, brightness_changed=True)
    )
    await wait_for_pending_steady_request(light, (400, None, None))
    release_first_write.set()

    await asyncio.wait_for(asyncio.gather(obsolete, latest), 1)

    assert payloads == [
        *first_payloads,
        *latest_payloads,
        telink.status_request(),
    ]
    assert light.effect2_state is not None
    assert light.effect2_state.intensity == 400


@pytest.mark.asyncio
async def test_rapid_pixel_brightness_completes_packets_then_sends_latest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pixel program is never abandoned half-written when superseded."""
    profile = get_fixture_profile_by_product_id("000F5")
    light = make_light(monkeypatch, profile=profile)
    for payload in pixelfx.effect(pixelfx.PixelEffect.COLOR_FADE):
        light._on_access_message(access_message(as_report(payload)))
    assert light.pixel_state is not None
    first_payloads = device._pixel_payloads(
        light.pixel_state,
        intensity=200,
        on=True,
    )
    latest_payloads = device._pixel_payloads(
        light.pixel_state,
        intensity=400,
        on=True,
    )
    first_write_started = asyncio.Event()
    release_first_write = asyncio.Event()
    payloads: list[bytes] = []

    async def send(payload: bytes, retries: int = 3) -> None:
        del retries
        payloads.append(payload)
        if payload == first_payloads[0]:
            first_write_started.set()
            await release_first_write.wait()
        elif payload == telink.status_request():
            for report in latest_payloads:
                light._on_access_message(access_message(as_report(report)))

    light._async_send = AsyncMock(side_effect=send)
    obsolete = asyncio.create_task(
        light.async_apply_turn_on(intensity=200, brightness_changed=True)
    )
    await first_write_started.wait()
    latest = asyncio.create_task(
        light.async_apply_turn_on(intensity=400, brightness_changed=True)
    )
    await wait_for_pending_steady_request(light, (400, None, None))
    release_first_write.set()

    await asyncio.wait_for(asyncio.gather(obsolete, latest), 1)

    assert payloads == [
        *first_payloads,
        *latest_payloads,
        telink.status_request(),
    ]
    assert light.pixel_state is not None
    assert light.pixel_state.intensity == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("first_kind", ["brightness", "cct"])
async def test_mixed_slider_updates_merge_partial_brightness_and_cct(
    monkeypatch: pytest.MonkeyPatch,
    first_kind: str,
) -> None:
    """A later partial patch cannot resurrect cached brightness or colour."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    light._state = light_state(is_hsi=False, on=True, intensity=100)
    first_write_started = asyncio.Event()
    release_first_write = asyncio.Event()
    payloads: list[bytes] = []
    first_payload = (
        telink.brightness(200) if first_kind == "brightness" else telink.cct(4509, 100)
    )

    async def send(payload: bytes, retries: int = 3) -> None:
        del retries
        payloads.append(payload)
        if payload == first_payload:
            first_write_started.set()
            await release_first_write.wait()
        elif payload == telink.status_request():
            light._on_access_message(
                access_message(as_report(telink.cct(4509, 200), on=True))
            )

    light._async_send = AsyncMock(side_effect=send)
    if first_kind == "brightness":
        first = asyncio.create_task(
            light.async_apply_turn_on(intensity=200, brightness_changed=True)
        )
    else:
        first = asyncio.create_task(
            light.async_apply_turn_on(
                intensity=100,
                brightness_changed=False,
                kelvin=4509,
            )
        )
    await first_write_started.wait()
    if first_kind == "brightness":
        latest = asyncio.create_task(
            light.async_apply_turn_on(
                intensity=100,
                brightness_changed=False,
                kelvin=4509,
            )
        )
    else:
        latest = asyncio.create_task(
            light.async_apply_turn_on(intensity=200, brightness_changed=True)
        )
    await wait_for_pending_steady_request(light, (200, 4509, None))
    release_first_write.set()

    await asyncio.wait_for(asyncio.gather(first, latest), 1)

    assert payloads == [
        first_payload,
        telink.cct(4509, 200),
        telink.status_request(),
    ]
    assert light.state is not None
    assert light.state.intensity == 200
    assert light.state.kelvin == 4500


@pytest.mark.asyncio
async def test_latest_colour_mode_replaces_superseded_cct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HS supersedes an unconfirmed CCT patch without losing brightness."""
    light = make_light(monkeypatch)
    light._state = light_state(is_hsi=False, on=True, intensity=100)
    first_write_started = asyncio.Event()
    release_first_write = asyncio.Event()
    payloads: list[bytes] = []

    async def send(payload: bytes, retries: int = 3) -> None:
        del retries
        payloads.append(payload)
        if payload == telink.cct(4500, 200):
            first_write_started.set()
            await release_first_write.wait()
        elif payload == telink.status_request():
            light._on_access_message(
                access_message(as_report(telink.hsi(180, 50, 200), on=True))
            )

    light._async_send = AsyncMock(side_effect=send)
    first = asyncio.create_task(
        light.async_apply_turn_on(
            intensity=200,
            brightness_changed=True,
            kelvin=4500,
        )
    )
    await first_write_started.wait()
    latest = asyncio.create_task(
        light.async_apply_turn_on(
            intensity=100,
            brightness_changed=False,
            hs_color=(180, 50),
        )
    )
    await wait_for_pending_steady_request(light, (200, None, (180, 50)))
    release_first_write.set()

    await asyncio.wait_for(asyncio.gather(first, latest), 1)

    assert payloads == [
        telink.cct(4500, 200),
        telink.hsi(180, 50, 200),
        telink.status_request(),
    ]
    assert light.state is not None
    assert light.state.is_hsi
    assert light.state.intensity == 200


@pytest.mark.asyncio
async def test_only_latest_slider_failure_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An obsolete request succeeds while the unsatisfied final value fails."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    light._state = light_state(is_hsi=False, on=True, intensity=100)
    first_write_started = asyncio.Event()
    release_first_write = asyncio.Event()
    status_requests = 0

    async def send(payload: bytes, retries: int = 3) -> None:
        nonlocal status_requests
        del retries
        if payload == telink.brightness(200):
            first_write_started.set()
            await release_first_write.wait()
        elif payload == telink.status_request():
            status_requests += 1
            light._on_access_message(
                access_message(as_report(telink.cct(4300, 100), on=True))
            )

    light._async_send = AsyncMock(side_effect=send)
    obsolete = asyncio.create_task(
        light.async_apply_turn_on(intensity=200, brightness_changed=True)
    )
    await first_write_started.wait()
    latest = asyncio.create_task(
        light.async_apply_turn_on(intensity=400, brightness_changed=True)
    )
    await wait_for_pending_steady_request(light, (400, None, None))
    release_first_write.set()

    await asyncio.wait_for(obsolete, 1)
    with pytest.raises(device.AmaranConnectionError, match="reported a different"):
        await asyncio.wait_for(latest, 1)

    assert status_requests == 3


@pytest.mark.asyncio
async def test_off_invalidates_queued_slider_updates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No obsolete slider packet can run after a later explicit OFF."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    light._state = light_state(is_hsi=False, on=True, intensity=100)
    first_write_started = asyncio.Event()
    release_first_write = asyncio.Event()
    payloads: list[bytes] = []

    async def send(payload: bytes, retries: int = 3) -> None:
        del retries
        payloads.append(payload)
        if payload == telink.brightness(200):
            first_write_started.set()
            await release_first_write.wait()
        elif payload == telink.status_request():
            light._on_access_message(
                access_message(as_report(telink.cct(4300, 100), on=False))
            )

    light._async_send = AsyncMock(side_effect=send)
    first = asyncio.create_task(
        light.async_apply_turn_on(intensity=200, brightness_changed=True)
    )
    await first_write_started.wait()
    queued = asyncio.create_task(
        light.async_apply_turn_on(intensity=300, brightness_changed=True)
    )
    await wait_for_pending_steady_request(light, (300, None, None))
    turn_off = asyncio.create_task(light.async_apply_turn_off())
    await asyncio.sleep(0)
    release_first_write.set()

    await asyncio.wait_for(asyncio.gather(first, queued, turn_off), 1)

    assert telink.brightness(300) not in payloads
    assert payloads == [
        telink.brightness(200),
        telink.onoff(False),
        telink.status_request(),
    ]
    assert light.state is not None and not light.state.on


@pytest.mark.asyncio
async def test_supersession_never_hides_a_transport_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only obsolete confirmation errors are suppressed, never failed writes."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    light._state = light_state(is_hsi=False, on=True, intensity=100)
    first_write_started = asyncio.Event()
    release_first_write = asyncio.Event()

    async def send(payload: bytes, retries: int = 3) -> None:
        del retries
        if payload == telink.brightness(200):
            first_write_started.set()
            await release_first_write.wait()
            raise device.AmaranConnectionError("transport write failed")
        if payload == telink.status_request():
            light._on_access_message(
                access_message(as_report(telink.cct(4300, 400), on=True))
            )

    light._async_send = AsyncMock(side_effect=send)
    failed = asyncio.create_task(
        light.async_apply_turn_on(intensity=200, brightness_changed=True)
    )
    await first_write_started.wait()
    latest = asyncio.create_task(
        light.async_apply_turn_on(intensity=400, brightness_changed=True)
    )
    await wait_for_pending_steady_request(light, (400, None, None))
    release_first_write.set()

    with pytest.raises(device.AmaranConnectionError, match="transport write failed"):
        await asyncio.wait_for(failed, 1)
    await asyncio.wait_for(latest, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "message"),
    [
        (device._PrimaryConfirmation.NO_REPORT, "did not return a report"),
        (device._PrimaryConfirmation.MISMATCHED, "reported a different state"),
    ],
)
async def test_final_light_confirmation_explains_failure_class(
    monkeypatch: pytest.MonkeyPatch,
    outcome: device._PrimaryConfirmation,
    message: str,
) -> None:
    """Latest failures distinguish missing reports from mismatching reports."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    light._state = light_state(is_hsi=False, on=True, intensity=100)
    light._last_primary_confirmation = outcome
    light._async_send = AsyncMock()
    light._async_confirm_primary_state = AsyncMock(return_value=False)

    with pytest.raises(device.AmaranConnectionError, match=message):
        await light.async_apply_turn_on(intensity=400, brightness_changed=True)


@pytest.mark.asyncio
async def test_primary_confirmation_classifies_no_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unanswered status query is distinct from a state mismatch."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    light._async_send = AsyncMock()

    result = await light._async_confirm_primary_state_result(
        lambda: False,
        attempts=1,
        timeout=0,
    )

    assert result is device._PrimaryConfirmation.NO_REPORT


@pytest.mark.asyncio
async def test_primary_confirmation_classifies_mismatching_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh but wrong status report receives its own diagnostic result."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)

    async def send(payload: bytes, retries: int = 3) -> None:
        del retries
        assert payload == telink.status_request()
        light._on_access_message(
            access_message(as_report(telink.cct(4300, 100), on=True))
        )

    light._async_send = AsyncMock(side_effect=send)

    result = await light._async_confirm_primary_state_result(
        lambda: False,
        attempts=1,
        timeout=0,
    )

    assert result is device._PrimaryConfirmation.MISMATCHED


@pytest.mark.asyncio
async def test_steady_turn_on_rejects_larger_intensity_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even a one-step raw change must fail across an HA brightness boundary."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    light._state = light_state(is_hsi=False, on=True, intensity=100)
    status_requests = 0

    async def send(payload: bytes, retries: int = 3) -> None:
        nonlocal status_requests
        del retries
        if payload == telink.status_request():
            status_requests += 1
            light._on_access_message(
                access_message(as_report(telink.cct(4500, 499), on=True))
            )

    light._async_send = AsyncMock(side_effect=send)

    with pytest.raises(device.AmaranConnectionError, match="requested light state"):
        await light.async_apply_turn_on(
            intensity=500,
            brightness_changed=True,
            kelvin=4500,
        )

    assert status_requests == 3


@pytest.mark.asyncio
async def test_steady_turn_on_confirms_truncated_command2_kelvin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Confirmation uses the app's positive-Kelvin integer division."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    light._state = light_state(is_hsi=False, on=True, intensity=100)

    async def send(payload: bytes, retries: int = 3) -> None:
        del retries
        if payload == telink.status_request():
            light._on_access_message(
                access_message(as_report(telink.cct(4509, 502), on=True))
            )

    light._async_send = AsyncMock(side_effect=send)

    await light.async_apply_turn_on(
        intensity=502,
        brightness_changed=True,
        kelvin=4509,
    )

    assert light.state is not None and light.state.kelvin == 4500


@pytest.mark.asyncio
async def test_turn_on_rejects_fresh_but_still_sleeping_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An acknowledged status that remains off cannot confirm turn_on."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    light._state = light_state(is_hsi=False, on=False)
    status_requests = 0

    async def send(payload: bytes, retries: int = 3) -> None:
        nonlocal status_requests
        del retries
        if payload == telink.status_request():
            status_requests += 1
            light._on_access_message(
                access_message(as_report(telink.cct(4300, 640), on=False))
            )

    light._async_send = AsyncMock(side_effect=send)

    with pytest.raises(device.AmaranConnectionError, match="requested light state"):
        await light.async_apply_turn_on(intensity=640, brightness_changed=False)

    assert status_requests == 3


@pytest.mark.asyncio
async def test_turn_off_rejects_fresh_but_still_on_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lost OFF command is surfaced even when status requests still reply."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    light._state = light_state(is_hsi=False, on=True)
    status_requests = 0

    async def send(payload: bytes, retries: int = 3) -> None:
        nonlocal status_requests
        del retries
        if payload == telink.status_request():
            status_requests += 1
            light._on_access_message(
                access_message(as_report(telink.cct(4300, 640), on=True))
            )

    light._async_send = AsyncMock(side_effect=send)

    with pytest.raises(device.AmaranConnectionError, match="did not confirm power off"):
        await light.async_apply_turn_off()

    assert status_requests == 3


@pytest.mark.asyncio
async def test_effect_confirmation_retries_until_fresh_state_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A delayed old mode cannot falsely confirm a proprietary FX write."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    light._state = light_state(is_hsi=False)
    status_requests = 0

    async def send(payload: bytes, retries: int = 3) -> None:
        nonlocal status_requests
        del retries
        if payload != telink.status_request():
            return
        status_requests += 1
        if status_requests == 1:
            response = as_report(telink.cct(4300, 640), on=True)
        else:
            response = as_report(telink.effect(telink.SystemEffect.TV, intensity=640))
        light._on_access_message(access_message(response))

    light._async_send = AsyncMock(side_effect=send)

    await light.async_apply_effect(telink.SystemEffect.TV)

    assert status_requests == 2
    assert light.effect_state is not None
    assert light.effect_state.effect is telink.SystemEffect.TV


@pytest.mark.asyncio
async def test_invalid_effect_is_a_controlled_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Arbitrary HA effect strings never leak a raw enum ValueError."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)

    with pytest.raises(device.AmaranConnectionError, match="not a supported effect"):
        await light.async_apply_effect("not-real")


@pytest.mark.asyncio
async def test_cataloged_system_effect2_selects_and_confirms_real_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A default-safe cmd34 effect accepts a brightness-equivalent report."""
    profile = get_fixture_profile_by_product_id("000G5")
    light = make_light(monkeypatch, profile=profile)
    light._proxy = Mock()
    light._state = light_state(is_hsi=True)
    latest: bytes | None = None

    async def send(payload: bytes, retries: int = 3) -> None:
        nonlocal latest
        del retries
        if payload[9] & 0x7F == systemfx2.CMD_SYSTEM_EFFECT_2:
            requested = systemfx2.decode_effect2(payload)
            assert requested is not None
            latest = as_report(
                systemfx2.effect2(
                    requested.effect,
                    on=requested.on,
                    intensity=501,
                    frequency=requested.frequency,
                    speed=requested.speed,
                    mode=requested.mode,
                    kelvin=requested.kelvin,
                    gm=requested.gm,
                    hue=requested.hue,
                    saturation=requested.saturation,
                    center_kelvin=requested.center_kelvin,
                )[0]
            )
            light._on_access_message(access_message(latest))
        elif payload == telink.status_request() and latest is not None:
            light._on_access_message(access_message(latest))

    light._async_send = AsyncMock(side_effect=send)

    await light.async_apply_effect("Lightning II", intensity=502)

    assert light.effect2_state is not None
    assert light.effect2_state.effect is systemfx2.SystemEffect2.LIGHTNING_II
    assert light.effect2_state.intensity == 501
    assert light.effect_state is light.effect2_state
    assert light.available


@pytest.mark.asyncio
async def test_sleeping_system_effect2_resumes_as_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Turning on a cached sleeping generation-II effect must set its ON state."""
    profile = get_fixture_profile_by_product_id("000G5")
    light = make_light(monkeypatch, profile=profile)
    sleeping = as_report(
        systemfx2.effect2(
            systemfx2.SystemEffect2.LIGHTNING_II,
            on=False,
            intensity=321,
            frequency=5,
            speed=5,
            mode=0,
            kelvin=4300,
            gm=100,
        )[0]
    )
    light._on_access_message(access_message(sleeping))
    reports: list[bytes] = []

    async def send(payload: bytes, retries: int = 3) -> None:
        del retries
        if payload[9] & 0x7F == systemfx2.CMD_SYSTEM_EFFECT_2:
            report = as_report(payload)
            reports.append(report)
            light._on_access_message(access_message(report))
        elif payload == telink.status_request() and reports:
            light._on_access_message(access_message(reports[-1]))

    light._async_send = AsyncMock(side_effect=send)

    await light.async_apply_turn_on(intensity=321, brightness_changed=False)

    assert reports
    assert systemfx2.decode_effect2(reports[-1]).on is True
    assert light.effect2_state is not None and light.effect2_state.on


@pytest.mark.asyncio
async def test_system_effect2_brightness_rejects_reset_preserved_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A same-effect rewrite must confirm every field in its complete packet."""
    profile = get_fixture_profile_by_product_id("000G5")
    light = make_light(monkeypatch, profile=profile)
    initial = as_report(
        systemfx2.effect2(
            systemfx2.SystemEffect2.LIGHTNING_II,
            intensity=321,
            frequency=5,
            speed=7,
            mode=1,
            hue=120,
            saturation=75,
            center_kelvin=5600,
        )[0]
    )
    light._on_access_message(access_message(initial))
    wrong_report: bytes | None = None

    async def send(payload: bytes, retries: int = 3) -> None:
        nonlocal wrong_report
        del retries
        if payload[9] & 0x7F == systemfx2.CMD_SYSTEM_EFFECT_2:
            requested = systemfx2.decode_effect2(payload)
            assert requested is not None
            wrong_report = as_report(
                systemfx2.effect2(
                    requested.effect,
                    on=True,
                    intensity=requested.intensity,
                    frequency=requested.frequency,
                    speed=1,
                    mode=requested.mode,
                    hue=requested.hue,
                    saturation=requested.saturation,
                    center_kelvin=requested.center_kelvin,
                )[0]
            )
            light._on_access_message(access_message(wrong_report))
        elif payload == telink.status_request() and wrong_report is not None:
            light._on_access_message(access_message(wrong_report))

    light._async_send = AsyncMock(side_effect=send)

    with pytest.raises(device.AmaranConnectionError, match="did not confirm"):
        await light.async_apply_turn_on(intensity=500, brightness_changed=True)


@pytest.mark.asyncio
async def test_system_effect2_parameter_accepts_ha_equivalent_intensity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cmd34 parameter rewrite normalizes only its global intensity."""
    profile = get_fixture_profile_by_product_id("000G5")
    light = make_light(monkeypatch, profile=profile)
    initial = as_report(
        systemfx2.effect2(
            systemfx2.SystemEffect2.LIGHTNING_II,
            intensity=502,
            frequency=5,
            speed=7,
            mode=1,
            hue=120,
            saturation=75,
            center_kelvin=5600,
        )[0]
    )
    light._on_access_message(access_message(initial))
    quantized = as_report(
        systemfx2.effect2(
            systemfx2.SystemEffect2.LIGHTNING_II,
            intensity=501,
            frequency=8,
            speed=7,
            mode=1,
            hue=120,
            saturation=75,
            center_kelvin=5600,
        )[0]
    )
    status_requests = 0

    async def send(payload: bytes, retries: int = 3) -> None:
        nonlocal status_requests
        del retries
        if payload == telink.status_request():
            status_requests += 1
            light._on_access_message(access_message(quantized))

    light._async_send = AsyncMock(side_effect=send)

    await light.async_set_effect_frequency(8)

    assert status_requests == 1
    assert light.effect2_state is not None
    assert light.effect2_state.intensity == 501
    assert light.effect2_state.frequency == 8
    assert light.effect2_state.speed == 7


@pytest.mark.asyncio
async def test_dual_page_system_effect2_is_merged_before_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Paparazzi II retains timing and colour pages as one device state."""
    light = make_light(monkeypatch, profile=get_fixture_profile_by_product_id("000F5"))
    light._state = light_state(is_hsi=True)
    reports: list[bytes] = []

    async def send(payload: bytes, retries: int = 3) -> None:
        del retries
        if payload[9] & 0x7F == systemfx2.CMD_SYSTEM_EFFECT_2:
            report = as_report(payload)
            reports.append(report)
            light._on_access_message(access_message(report))
        elif payload == telink.status_request():
            for report in reports:
                light._on_access_message(access_message(report))

    light._async_send = AsyncMock(side_effect=send)

    await light.async_apply_effect("Paparazzi II", intensity=432)

    assert len(reports) == 2
    assert light.effect2_state is not None
    assert light.effect2_state.package_type is None
    assert light.effect2_state.intensity == 432
    assert light.effect2_state.gap_time == 60
    assert light.effect2_state.min_gap_time == 20
    assert light.effect2_state.mode == 1
    assert light.effect2_state.saturation == 75


@pytest.mark.asyncio
async def test_system_effect2_hsi_parameters_preserve_complete_reported_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generation-II rate, hue, and saturation writes round-trip exactly."""
    light = make_light(monkeypatch, profile=get_fixture_profile_by_product_id("000G5"))
    latest = as_report(
        systemfx2.effect2(
            systemfx2.SystemEffect2.LIGHTNING_II,
            intensity=432,
            frequency=5,
            speed=7,
            mode=1,
            hue=120,
            saturation=75,
            center_kelvin=5600,
        )[0]
    )
    light._on_access_message(access_message(latest))

    async def send(payload: bytes, retries: int = 3) -> None:
        nonlocal latest
        del retries
        if payload[9] & 0x7F == systemfx2.CMD_SYSTEM_EFFECT_2:
            latest = as_report(payload)
            light._on_access_message(access_message(latest))
        elif payload == telink.status_request():
            light._on_access_message(access_message(latest))

    light._async_send = AsyncMock(side_effect=send)

    await light.async_set_effect_frequency(8)
    await light.async_set_effect_hue(231)
    await light.async_set_effect_saturation(44)

    assert light.effect2_state == systemfx2.SystemEffect2State(
        on=True,
        effect=systemfx2.SystemEffect2.LIGHTNING_II,
        state=1,
        intensity=432,
        frequency=8,
        speed=7,
        mode=1,
        hue=231,
        saturation=44,
        center_kelvin=5600,
    )


@pytest.mark.asyncio
async def test_system_effect2_cct_and_gm_use_profile_ranges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generation-II CCT pages expose the same normalized tint as steady mode."""
    light = make_light(monkeypatch, profile=get_fixture_profile_by_product_id("000G5"))
    latest = as_report(
        systemfx2.effect2(
            systemfx2.SystemEffect2.LIGHTNING_II,
            intensity=500,
            frequency=5,
            speed=5,
            mode=0,
            kelvin=4300,
            gm=100,
        )[0]
    )
    light._on_access_message(access_message(latest))

    async def send(payload: bytes, retries: int = 3) -> None:
        nonlocal latest
        del retries
        if payload[9] & 0x7F == systemfx2.CMD_SYSTEM_EFFECT_2:
            latest = as_report(payload)
            light._on_access_message(access_message(latest))
        elif payload == telink.status_request():
            light._on_access_message(access_message(latest))

    light._async_send = AsyncMock(side_effect=send)

    await light.async_set_effect_kelvin(4451)
    await light.async_set_effect_gm(-2.5)

    assert light.effect2_state is not None
    assert light.effect2_state.kelvin == 4450
    assert light.effect2_state.gm == 75
    assert light.effect2_state.intensity == 500
    assert light.effect2_state.frequency == 5
    assert light.effect2_state.speed == 5


@pytest.mark.asyncio
async def test_generation_three_effect_stays_cataloged_but_not_selectable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unproven command-34 defaults cannot be sent from Home Assistant."""
    light = make_light(monkeypatch, profile=get_fixture_profile_by_product_id("000F5"))

    with pytest.raises(device.AmaranConnectionError, match="not a supported effect"):
        await light.async_apply_effect("Lightning III")


@pytest.mark.asyncio
async def test_cataloged_pixel_effect_merges_pages_and_preserves_brightness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Command-33 programs behave as one HA effect without losing colour pages."""
    profile = get_fixture_profile_by_product_id("000F5")
    light = make_light(monkeypatch, profile=profile)
    light._state = light_state(is_hsi=True)
    reports: list[bytes] = []

    async def send(payload: bytes, retries: int = 3) -> None:
        del retries
        if payload[9] & 0x7F == pixelfx.CMD_PIXEL_EFFECT:
            report = as_report(payload)
            reports.append(report)
            light._on_access_message(access_message(report))
        elif payload == telink.status_request() and reports:
            # Real status refreshes commonly return only the control page.
            light._on_access_message(access_message(reports[-1]))

    light._async_send = AsyncMock(side_effect=send)

    await light.async_apply_effect("Two Pixel Chase", intensity=321)

    assert light.pixel_state is not None
    assert light.pixel_state.effect is pixelfx.PixelEffect.TWO_PIXEL_CHASE
    assert light.pixel_state.on
    assert light.pixel_state.intensity == 321
    assert len(light.pixel_state.pages) == 4
    original_colors = tuple(
        (page.serial, page.light_mode, page.hue, page.cct_raw)
        for page in light.pixel_state.pages
        if page.packet_type is pixelfx.PixelPacketType.COLOR
    )

    reports.clear()
    await light.async_apply_turn_on(
        intensity=654,
        brightness_changed=True,
    )

    assert light.pixel_state is not None
    assert light.pixel_state.intensity == 654
    assert (
        tuple(
            (page.serial, page.light_mode, page.hue, page.cct_raw)
            for page in light.pixel_state.pages
            if page.packet_type is pixelfx.PixelPacketType.COLOR
        )
        == original_colors
    )
    assert all(
        page.brightness == 654
        for page in light.pixel_state.pages
        if page.brightness is not None
    )


def test_pixel_rebuild_preserves_a_reported_nondefault_color_count() -> None:
    """Brightness updates cannot collapse a customized Fade back to two slots."""
    packets = (
        *(
            pixelfx.color(
                pixelfx.PixelEffect.COLOR_FADE,
                playback=pixelfx.PixelPlayback.CONTINUE,
                serial=serial,
                brightness=200,
                light_mode=pixelfx.PixelLightMode.HSI,
                hue=hue,
                saturation=75,
                hsi_cct_raw=112,
            )
            for serial, hue in enumerate((10, 120, 250))
        ),
        pixelfx.color_fade(
            playback=pixelfx.PixelPlayback.RUNNING,
            color_count=3,
            direction=0,
            speed=345,
        ),
    )
    state = device._decode_pixel_sequence(packets)

    rebuilt = device._decode_pixel_sequence(
        device._pixel_payloads(state, intensity=777, on=True)
    )

    assert len(rebuilt.pages) == 4
    assert rebuilt.intensity == 777
    control = next(
        page
        for page in rebuilt.pages
        if page.packet_type is pixelfx.PixelPacketType.CONTROL
    )
    assert (control.color_count, control.direction, control.speed) == (3, 0, 345)
    assert [
        page.hue
        for page in rebuilt.pages
        if page.packet_type is pixelfx.PixelPacketType.COLOR
    ] == [10, 120, 250]


def test_pixel_rebuild_drops_colors_above_a_shrunk_color_count() -> None:
    """A Fade changed from three to two colours must not resend stale slot 2."""
    packets = (
        *(
            pixelfx.color(
                pixelfx.PixelEffect.COLOR_FADE,
                playback=pixelfx.PixelPlayback.CONTINUE,
                serial=serial,
                brightness=200,
                light_mode=pixelfx.PixelLightMode.HSI,
                hue=hue,
                saturation=75,
                hsi_cct_raw=112,
            )
            for serial, hue in enumerate((10, 120, 250))
        ),
        pixelfx.color_fade(
            playback=pixelfx.PixelPlayback.RUNNING,
            color_count=3,
            direction=0,
            speed=345,
        ),
    )
    state = device._decode_pixel_sequence(packets)
    state = device._merge_pixel_page(
        state,
        pixelfx.decode(
            pixelfx.color_fade(
                playback=pixelfx.PixelPlayback.RUNNING,
                color_count=2,
                direction=0,
                speed=345,
            )
        ),
    )

    rebuilt = device._decode_pixel_sequence(
        device._pixel_payloads(state, intensity=None, on=True)
    )

    assert [
        page.serial
        for page in rebuilt.pages
        if page.packet_type is pixelfx.PixelPacketType.COLOR
    ] == [0, 1]
    control = next(
        page
        for page in rebuilt.pages
        if page.packet_type is pixelfx.PixelPacketType.CONTROL
    )
    assert control.color_count == 2


def test_pixel_chase_group_controls_required_color_page_count() -> None:
    """Double groups retain the app-required extra chase color pages."""
    selected = pixelfx.PixelEffect.TWO_PIXEL_CHASE
    packets = (
        *(
            pixelfx.color(
                selected,
                playback=pixelfx.PixelPlayback.CONTINUE,
                serial=serial,
                brightness=180,
                light_mode=pixelfx.PixelLightMode.HSI,
                hue=serial * 60,
                saturation=100,
                hsi_cct_raw=112,
            )
            for serial in range(5)
        ),
        pixelfx.chase(
            selected,
            playback=pixelfx.PixelPlayback.RUNNING,
            group=1,
            direction=1,
            speed=100,
            pixel_length=1,
        ),
    )
    state = device._decode_pixel_sequence(packets)

    assert device._pixel_pages_complete(state)
    assert not device._pixel_pages_complete(
        device._decode_pixel_sequence((*packets[:4], packets[-1]))
    )
    rebuilt = device._decode_pixel_sequence(
        device._pixel_payloads(state, intensity=500, on=True)
    )
    assert (
        len(
            [
                page
                for page in rebuilt.pages
                if page.packet_type is pixelfx.PixelPacketType.COLOR
            ]
        )
        == 5
    )


def test_pixel_chase_group_shrink_drops_stale_color_pages() -> None:
    """Changing a chase from Double to Single must omit obsolete colors."""
    selected = pixelfx.PixelEffect.TWO_PIXEL_CHASE
    packets = (
        *(
            pixelfx.color(
                selected,
                playback=pixelfx.PixelPlayback.CONTINUE,
                serial=serial,
                brightness=180,
                light_mode=pixelfx.PixelLightMode.HSI,
                hue=serial * 60,
                saturation=100,
                hsi_cct_raw=112,
            )
            for serial in range(5)
        ),
        pixelfx.chase(
            selected,
            playback=pixelfx.PixelPlayback.RUNNING,
            group=1,
            direction=1,
            speed=100,
            pixel_length=1,
        ),
    )
    state = device._decode_pixel_sequence(packets)
    state = device._merge_pixel_page(
        state,
        pixelfx.decode(
            pixelfx.chase(
                selected,
                playback=pixelfx.PixelPlayback.RUNNING,
                group=0,
                direction=1,
                speed=100,
                pixel_length=1,
            )
        ),
    )

    rebuilt = device._decode_pixel_sequence(
        device._pixel_payloads(state, intensity=None, on=True)
    )
    assert [
        page.serial
        for page in rebuilt.pages
        if page.packet_type is pixelfx.PixelPacketType.COLOR
    ] == [0, 1, 2]


@pytest.mark.asyncio
async def test_idempotent_pixel_turn_on_does_not_replace_partial_program(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An already-on control report must not trigger a default program rewrite."""
    light = make_light(monkeypatch, profile=get_fixture_profile_by_product_id("000F5"))
    control = pixelfx.color_fade(
        playback=pixelfx.PixelPlayback.RUNNING,
        color_count=3,
        direction=0,
        speed=345,
    )
    report = as_report(control)
    light._on_access_message(access_message(report))

    async def send(payload: bytes, retries: int = 3) -> None:
        del retries
        if payload == telink.status_request():
            light._on_access_message(access_message(report))

    light._async_send = AsyncMock(side_effect=send)

    await light.async_apply_turn_on(intensity=180, brightness_changed=False)

    assert [call.args[0] for call in light._async_send.await_args_list] == [
        telink.status_request()
    ]


@pytest.mark.asyncio
async def test_pixel_brightness_rejects_an_incomplete_reported_program(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Brightness cannot safely invent missing command-33 colour pages."""
    light = make_light(monkeypatch, profile=get_fixture_profile_by_product_id("000F5"))
    control = pixelfx.color_fade(
        playback=pixelfx.PixelPlayback.RUNNING,
        color_count=3,
        direction=0,
        speed=345,
    )
    light._on_access_message(access_message(as_report(control)))
    light._async_send = AsyncMock()

    with pytest.raises(device.AmaranConnectionError, match="complete pixel program"):
        await light.async_apply_turn_on(intensity=500, brightness_changed=True)

    light._async_send.assert_not_awaited()


@pytest.mark.parametrize(
    ("requested_intensity", "reported_intensity", "succeeds"),
    [(502, 501, True), (500, 499, False)],
)
@pytest.mark.asyncio
async def test_rainbow_brightness_confirmation_uses_ha_buckets(
    monkeypatch: pytest.MonkeyPatch,
    requested_intensity: int,
    reported_intensity: int,
    succeeds: bool,
) -> None:
    """Rainbow accepts quantization but not another HA brightness value."""
    light = make_light(monkeypatch, profile=get_fixture_profile_by_product_id("000F5"))
    initial = pixelfx.effect(pixelfx.PixelEffect.RAINBOW)[0]
    light._on_access_message(access_message(as_report(initial)))
    reported = as_report(
        pixelfx.rainbow(
            playback=pixelfx.PixelPlayback.RUNNING,
            brightness=reported_intensity,
            direction=0,
            speed=100,
        )
    )
    status_requests = 0

    async def send(payload: bytes, retries: int = 3) -> None:
        nonlocal status_requests
        del retries
        if payload == telink.status_request():
            status_requests += 1
            light._on_access_message(access_message(reported))

    light._async_send = AsyncMock(side_effect=send)

    if succeeds:
        await light.async_apply_turn_on(
            intensity=requested_intensity,
            brightness_changed=True,
        )
        assert status_requests == 1
        assert light.pixel_state is not None
        assert light.pixel_state.intensity == reported_intensity
    else:
        with pytest.raises(device.AmaranConnectionError, match="did not confirm"):
            await light.async_apply_turn_on(
                intensity=requested_intensity,
                brightness_changed=True,
            )
        assert status_requests == 3


@pytest.mark.parametrize(
    ("requested_intensity", "reported_intensity", "succeeds"),
    [(502, 501, True), (500, 499, False), (502, None, True)],
)
@pytest.mark.asyncio
async def test_multipage_pixel_brightness_confirmation_uses_fresh_pages(
    monkeypatch: pytest.MonkeyPatch,
    requested_intensity: int,
    reported_intensity: int | None,
    succeeds: bool,
) -> None:
    """Fresh color pages are checked; a control-only reply remains supported."""
    light = make_light(monkeypatch, profile=get_fixture_profile_by_product_id("000F5"))
    for payload in pixelfx.effect(pixelfx.PixelEffect.COLOR_FADE):
        light._on_access_message(access_message(as_report(payload)))
    assert light.pixel_state is not None
    reported_payloads = device._pixel_payloads(
        light.pixel_state,
        intensity=(
            requested_intensity if reported_intensity is None else reported_intensity
        ),
        on=True,
    )
    if reported_intensity is None:
        reported_payloads = (reported_payloads[-1],)
    status_requests = 0

    async def send(payload: bytes, retries: int = 3) -> None:
        nonlocal status_requests
        del retries
        if payload == telink.status_request():
            status_requests += 1
            for reported_payload in reported_payloads:
                light._on_access_message(access_message(as_report(reported_payload)))

    light._async_send = AsyncMock(side_effect=send)

    if succeeds:
        await light.async_apply_turn_on(
            intensity=requested_intensity,
            brightness_changed=True,
        )
        assert status_requests == 1
    else:
        with pytest.raises(device.AmaranConnectionError, match="did not confirm"):
            await light.async_apply_turn_on(
                intensity=requested_intensity,
                brightness_changed=True,
            )
        assert status_requests == 3


def test_pixel_continue_page_has_unknown_power_state() -> None:
    """A continuation-only colour page must not be published as on or off."""
    state = device._decode_pixel_sequence(
        (
            pixelfx.color(
                pixelfx.PixelEffect.COLOR_FADE,
                playback=pixelfx.PixelPlayback.CONTINUE,
                serial=0,
                brightness=180,
                light_mode=pixelfx.PixelLightMode.HSI,
                hue=0,
                saturation=100,
                hsi_cct_raw=112,
            ),
        )
    )

    assert state.on is None


@pytest.mark.asyncio
async def test_stale_pixel_control_cannot_confirm_a_fresh_color_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A continuation page cannot reuse an old RUNNING page as write proof."""
    light = make_light(monkeypatch, profile=get_fixture_profile_by_product_id("000F5"))
    initial = pixelfx.effect(pixelfx.PixelEffect.COLOR_FADE)
    for payload in initial:
        light._on_access_message(access_message(as_report(payload)))
    assert light.pixel_state is not None and light.pixel_state.on

    stale_color = as_report(initial[0])

    async def send(payload: bytes, retries: int = 3) -> None:
        del retries
        if payload == telink.status_request():
            light._on_access_message(access_message(stale_color))

    light._async_send = AsyncMock(side_effect=send)

    with pytest.raises(device.AmaranConnectionError, match="did not confirm"):
        await light.async_apply_turn_on(intensity=777, brightness_changed=True)


@pytest.mark.asyncio
async def test_pixel_only_profile_can_leave_effect_through_virtual_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shared virtual off item restores steady output for pixel programs."""
    profile = replace(get_fixture_profile_by_product_id("400G5"), effects=())
    assert profile.effects == ()
    assert profile.pixel_effects
    light = make_light(monkeypatch, profile=profile)
    light._state = light_state(is_hsi=False)
    light._pixel_state = device._decode_pixel_sequence(
        pixelfx.effect(pixelfx.PixelEffect.RAINBOW)
    )
    light._async_send = AsyncMock()
    light._async_confirm_primary_state = AsyncMock(return_value=True)

    await light.async_apply_effect("off", intensity=500)

    payloads = [call.args[0] for call in light._async_send.await_args_list]
    assert payloads == [telink.cct(4300, 500, 0), telink.onoff(True)]
    light._async_confirm_primary_state.assert_awaited_once()


def test_pixel_reports_are_profile_gated_and_write_echoes_are_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generic devices and relayed set packets cannot impersonate pixel state."""
    payload = pixelfx.effect(pixelfx.PixelEffect.RAINBOW)[0]
    generic = make_light(monkeypatch)
    generic._on_access_message(access_message(as_report(payload)))
    assert generic.pixel_state is None

    cataloged = make_light(
        monkeypatch, profile=get_fixture_profile_by_product_id("000F5")
    )
    cataloged._on_access_message(access_message(payload))
    assert cataloged.pixel_state is None

    cataloged._on_access_message(access_message(as_report(payload)))
    assert cataloged.pixel_state is not None
    assert cataloged.effect_state is cataloged.pixel_state


@pytest.mark.asyncio
async def test_turn_off_stops_pixel_program_then_sends_safety_power_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pixel playback follows the app's OFF build without replacing hard OFF."""
    light = make_light(monkeypatch, profile=get_fixture_profile_by_product_id("000F5"))
    light._pixel_state = device._decode_pixel_sequence(
        pixelfx.effect(pixelfx.PixelEffect.COLOR_FADE)
    )
    light._async_send = AsyncMock()
    light.async_turn_off = AsyncMock()
    light._async_confirm_primary_state = AsyncMock(return_value=True)

    await light.async_apply_turn_off()

    assert [call.args[0] for call in light._async_send.await_args_list] == list(
        pixelfx.effect(pixelfx.PixelEffect.COLOR_FADE, on=False)
    )
    light.async_turn_off.assert_awaited_once_with()
    light._async_confirm_primary_state.assert_awaited_once()


@pytest.mark.asyncio
async def test_pixel_stop_failure_cannot_block_safety_power_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed optional STOP sequence still reaches the normal OFF command."""
    light = make_light(monkeypatch, profile=get_fixture_profile_by_product_id("000F5"))
    light._pixel_state = device._decode_pixel_sequence(
        pixelfx.effect(pixelfx.PixelEffect.RAINBOW)
    )
    light._async_send = AsyncMock(
        side_effect=device.AmaranConnectionError("pixel link failed")
    )
    light.async_turn_off = AsyncMock()
    light._async_confirm_primary_state = AsyncMock(return_value=True)

    with pytest.raises(device.AmaranConnectionError, match="was turned off"):
        await light.async_apply_turn_off()

    light.async_turn_off.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_virtual_effect_off_restores_steady_cct_without_effect_15(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Leaving FX uses the app-proven CCT transition, not orphan effect ID 15."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    light._state = light_state(is_hsi=False, gm=1)
    light._preferred_gm = 1
    light._effect_state = telink.EffectState(
        on=True,
        effect=telink.SystemEffect.TV,
        intensity=200,
        frequency=5,
    )
    light._async_send = AsyncMock()
    light._async_confirm_primary_state = AsyncMock(return_value=True)

    await light.async_apply_effect("off", intensity=500)

    payloads = [call.args[0] for call in light._async_send.await_args_list]
    assert payloads == [telink.cct(4300, 500, 1), telink.onoff(True)]
    assert telink.effect_off() not in payloads
    light._async_confirm_primary_state.assert_awaited_once()


@pytest.mark.asyncio
async def test_boost_modal_state_and_report_confirmed_fan_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Boost mirrors the app's modal writes; fan still requires a full report."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    light._state = light_state(is_hsi=False)
    light._fan_state = telink.decode_fan(fan_report(telink.FanMode.SMART))
    light._async_refresh_state = AsyncMock(return_value=True)
    monkeypatch.setattr(device.asyncio, "sleep", AsyncMock())
    selected_fan = telink.FanMode.SMART

    async def send(payload: bytes, retries: int = 3) -> None:
        nonlocal selected_fan
        del retries
        command = payload[9] & 0x7F
        if command == telink.CMD_FAN and payload[9] & 0x80:
            decoded = telink.decode_fan(payload)
            assert decoded is not None
            selected_fan = decoded.mode
        elif command == telink.CMD_FAN:
            light._on_access_message(access_message(fan_report(selected_fan)))
        elif command == telink.CMD_STATUS_REQUEST:
            light._on_access_message(
                access_message(as_report(telink.cct(4300, 640), on=True))
            )

    light._async_send = AsyncMock(side_effect=send)

    await light.async_set_boost(True)
    await light.async_set_boost(False)
    await light.async_set_fan_mode("silent")

    assert light.boost_state is not None and not light.boost_state.enabled
    assert light.boost_state.kelvin == 4300
    assert light.fan_state is not None
    assert light.fan_state.mode is telink.FanMode.SILENT
    light._async_refresh_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_boost_tracks_modal_state_without_a_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ace matches the APK's write-only Boost dialog protocol."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    light._state = light_state(is_hsi=False)
    light._fan_state = telink.decode_fan(fan_report(telink.FanMode.SMART))

    light._async_send = AsyncMock()
    light._async_refresh_state = AsyncMock(return_value=True)

    await light.async_set_boost(True)

    assert light.boost_state == telink.BoostState(True, 4300, 100)
    light._async_send.assert_any_await(telink.boost(True, 4300, 100))


@pytest.mark.asyncio
async def test_explicit_boost_off_restores_exact_cct_brightness_and_power(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Boost dismissal restores the primary CCT look that command 70 replaced."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    original = replace(
        light_state(is_hsi=False, gm=2, intensity=640),
        kelvin=4250,
    )
    light._state = original
    light._preferred_gm = 2
    light._async_send = AsyncMock()
    light._async_refresh_state = AsyncMock(return_value=True)

    async def confirm(predicate: Any, **_kwargs: Any) -> bool:
        light._state = original
        light._effect_state = None
        light._effect2_state = None
        light._pixel_state = None
        return predicate()

    light._async_confirm_primary_state = AsyncMock(side_effect=confirm)

    await light.async_set_boost(True)
    # A primary report while the modal output is active may describe Boost's
    # temporary look. It must not replace the saved steady state.
    light._state = replace(original, intensity=1000, kelvin=5500)
    await light.async_set_boost(False)

    payloads = [call.args[0] for call in light._async_send.await_args_list]
    assert payloads == [
        telink.boost(True, 4250, 100),
        telink.boost(False, 4250, 100),
        telink.cct(4250, 640, 2),
        telink.onoff(True),
    ]
    assert light.boost_state == telink.BoostState(False, 4250, 100)
    assert light._boost_output_snapshot is None
    assert light.preferred_gm == 2
    light._async_refresh_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_boost_off_restores_a_sleeping_steady_output_without_waking_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A light that was off before Boost returns to off with its saved CCT."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    original = replace(
        light_state(is_hsi=False, on=False, intensity=310),
        kelvin=4100,
    )
    light._state = original
    light._async_send = AsyncMock()

    async def confirm(predicate: Any, **_kwargs: Any) -> bool:
        light._state = original
        return predicate()

    light._async_confirm_primary_state = AsyncMock(side_effect=confirm)

    await light.async_set_boost(True)
    await light.async_set_boost(False)

    payloads = [call.args[0] for call in light._async_send.await_args_list]
    assert payloads[-2:] == [telink.cct(4100, 310), telink.onoff(False)]
    assert light.state is not None and not light.state.on


@pytest.mark.asyncio
async def test_boost_off_restores_hsi_and_preserves_steady_gm_preference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A modal CCT preview cannot replace the prior HSI look or remembered tint."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    original = light_state(is_hsi=True, intensity=525)
    light._state = original
    light._preferred_gm = -3
    light._async_send = AsyncMock()

    async def confirm(predicate: Any, **_kwargs: Any) -> bool:
        light._state = original
        return predicate()

    light._async_confirm_primary_state = AsyncMock(side_effect=confirm)

    await light.async_set_boost(True)
    light._preferred_gm = 0
    await light.async_set_boost(False)

    payloads = [call.args[0] for call in light._async_send.await_args_list]
    assert payloads[-2:] == [telink.hsi(120, 75, 525), telink.onoff(True)]
    assert light.preferred_gm == -3


@pytest.mark.asyncio
async def test_boost_off_restores_the_exact_active_legacy_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closing Boost returns to an effect rather than its stale steady fallback."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    light._state = replace(light_state(is_hsi=False), kelvin=4200)
    effect_payload = telink.effect(
        telink.SystemEffect.TV,
        intensity=280,
        frequency=7,
        variant=2,
    )
    original_effect = telink.decode_effect(effect_payload)
    assert original_effect is not None
    light._effect_state = original_effect
    light._async_send = AsyncMock()

    async def confirm(predicate: Any, **_kwargs: Any) -> bool:
        light._effect_state = original_effect
        light._effect2_state = None
        light._pixel_state = None
        return predicate()

    light._async_confirm_primary_state = AsyncMock(side_effect=confirm)

    await light.async_set_boost(True)
    light._effect_state = None
    await light.async_set_boost(False)

    payloads = [call.args[0] for call in light._async_send.await_args_list]
    assert payloads[-1] == effect_payload
    assert light.effect_state == original_effect


@pytest.mark.asyncio
async def test_boost_off_restores_a_sleeping_effect_without_waking_the_light(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale steady ON cache cannot wake an effect that was powered down."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    light._state = replace(light_state(is_hsi=False, on=True), kelvin=4200)
    effect_payload = telink.effect(
        telink.SystemEffect.TV,
        intensity=280,
        frequency=7,
        variant=2,
    )
    active_effect = telink.decode_effect(effect_payload)
    assert active_effect is not None
    sleeping_effect = replace(active_effect, on=False)
    light._effect_state = sleeping_effect
    light._async_send = AsyncMock()

    async def confirm(predicate: Any, **_kwargs: Any) -> bool:
        light._effect_state = sleeping_effect
        light._effect2_state = None
        light._pixel_state = None
        return predicate()

    light._async_confirm_primary_state = AsyncMock(side_effect=confirm)

    await light.async_set_boost(True)
    light._effect_state = None
    await light.async_set_boost(False)

    payloads = [call.args[0] for call in light._async_send.await_args_list]
    assert payloads[-2:] == [effect_payload, telink.onoff(False)]
    assert light.effect_state == sleeping_effect
    assert light._boost_output_snapshot is None


@pytest.mark.asyncio
async def test_boost_restore_failure_stays_actionable_and_off_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unconfirmed restore keeps the modal session retryable from the switch."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    original = light_state(is_hsi=False)
    light._state = original
    light._async_send = AsyncMock()
    light._async_confirm_primary_state = AsyncMock(side_effect=[False, True])

    await light.async_set_boost(True)
    with pytest.raises(device.AmaranConnectionError, match="previous light state"):
        await light.async_set_boost(False)

    assert light.boost_state is not None and light.boost_state.enabled
    assert light._boost_output_snapshot is not None

    await light.async_set_boost(False)

    assert light.boost_state is not None and not light.boost_state.enabled
    assert light._boost_output_snapshot is None
    assert [
        payload
        for payload in (call.args[0] for call in light._async_send.await_args_list)
        if (payload[9] & 0x7F) == telink.CMD_BOOST
        and not telink.decode_boost(payload).enabled
    ] == [telink.boost(False, 4300, 100), telink.boost(False, 4300, 100)]


@pytest.mark.asyncio
async def test_boost_snapshot_and_assumed_session_survive_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A link loss cannot make an HA-started Boost session impossible to close."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    original = replace(light_state(is_hsi=False), kelvin=4200)
    light._state = original
    light._async_send = AsyncMock()

    await light.async_set_boost(True)
    light._clear_report_state()

    assert light.boost_state is None
    assert light._boost_output_snapshot is not None
    light._on_access_message(access_message(as_report(telink.cct(5500, 1000), on=True)))
    assert light.boost_state is not None and light.boost_state.enabled

    async def confirm(predicate: Any, **_kwargs: Any) -> bool:
        light._state = original
        return predicate()

    light._async_confirm_primary_state = AsyncMock(side_effect=confirm)
    await light.async_set_boost(False)

    payloads = [call.args[0] for call in light._async_send.await_args_list]
    assert payloads[-4:] == [
        telink.boost(True, 4200, 100),
        telink.boost(False, 5500, 100),
        telink.cct(4200, 640),
        telink.onoff(True),
    ]
    assert light._boost_output_snapshot is None


@pytest.mark.asyncio
async def test_primary_light_change_abandons_boost_snapshot_without_restoring_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new primary request supersedes, rather than briefly restoring, old CCT."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    original = replace(light_state(is_hsi=False), kelvin=4200)
    requested = replace(original, intensity=700, kelvin=5000)
    light._state = original
    light._async_send = AsyncMock()

    await light.async_set_boost(True)

    async def confirm(predicate: Any, **_kwargs: Any) -> bool:
        light._state = requested
        return predicate()

    light._async_confirm_primary_state = AsyncMock(side_effect=confirm)
    await light.async_apply_turn_on(
        intensity=700,
        brightness_changed=True,
        kelvin=5000,
    )

    payloads = [call.args[0] for call in light._async_send.await_args_list]
    assert payloads == [
        telink.boost(True, 4200, 100),
        telink.boost(False, 4200, 100),
        telink.cct(5000, 700),
    ]
    assert light._boost_output_snapshot is None


@pytest.mark.asyncio
async def test_brightness_while_boost_merges_with_the_pre_boost_colour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partial brightness patch cannot make Boost's temporary CCT durable."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    original = replace(
        light_state(is_hsi=False, intensity=400),
        kelvin=2700,
    )
    light._state = original
    light._async_send = AsyncMock()

    await light.async_set_boost(True)
    light._state = replace(original, intensity=1000, kelvin=5500)

    async def confirm(predicate: Any, **_kwargs: Any) -> bool:
        light._state = replace(original, intensity=700)
        return predicate()

    light._async_confirm_primary_state = AsyncMock(side_effect=confirm)
    await light.async_apply_turn_on(intensity=700, brightness_changed=True)

    payloads = [call.args[0] for call in light._async_send.await_args_list]
    assert payloads == [
        telink.boost(True, 2700, 100),
        telink.boost(False, 2700, 100),
        telink.cct(2700, 700),
        telink.onoff(True),
    ]
    assert light.state is not None
    assert light.state.kelvin == 2700
    assert light.state.intensity == 700
    assert light._boost_output_snapshot is None


@pytest.mark.asyncio
async def test_colour_only_change_while_boost_keeps_pre_boost_brightness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CCT patch does not inherit Boost's temporary full intensity."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    original = replace(
        light_state(is_hsi=False, intensity=400),
        kelvin=2700,
    )
    expected = replace(original, kelvin=4500)
    light._state = original
    light._async_send = AsyncMock()

    await light.async_set_boost(True)
    light._state = replace(original, intensity=1000, kelvin=5500)

    async def confirm(predicate: Any, **_kwargs: Any) -> bool:
        light._state = expected
        return predicate()

    light._async_confirm_primary_state = AsyncMock(side_effect=confirm)
    await light.async_apply_turn_on(
        intensity=1000,
        brightness_changed=False,
        kelvin=4500,
    )

    payloads = [call.args[0] for call in light._async_send.await_args_list]
    assert payloads == [
        telink.boost(True, 2700, 100),
        telink.boost(False, 2700, 100),
        telink.cct(4500, 400),
    ]
    assert light.state == expected
    assert light._boost_output_snapshot is None


@pytest.mark.asyncio
async def test_effect_off_while_boost_restores_the_pre_boost_steady_look(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The virtual effect-off action cannot persist command 70's CCT."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    original = replace(
        light_state(is_hsi=False, intensity=400),
        kelvin=2700,
    )
    light._state = original
    light._async_send = AsyncMock()

    await light.async_set_boost(True)
    light._state = replace(original, intensity=1000, kelvin=5500)

    async def confirm(predicate: Any, **_kwargs: Any) -> bool:
        light._state = original
        return predicate()

    light._async_confirm_primary_state = AsyncMock(side_effect=confirm)
    await light.async_apply_effect("off")

    payloads = [call.args[0] for call in light._async_send.await_args_list]
    assert payloads == [
        telink.boost(True, 2700, 100),
        telink.boost(False, 2700, 100),
        telink.cct(2700, 400),
        telink.onoff(True),
    ]
    assert light.state == original
    assert light._boost_output_snapshot is None


@pytest.mark.asyncio
async def test_gm_change_while_boost_restores_the_pre_boost_cct_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tint adjustment applies to the old steady CCT, not Boost's CCT."""
    profile = get_fixture_profile("ace_25c")
    light = make_light(monkeypatch, profile=profile)
    original = replace(
        light_state(is_hsi=False, intensity=400),
        kelvin=2700,
    )
    expected = replace(original, gm=2)
    light._state = original
    light._async_send = AsyncMock()

    await light.async_set_boost(True)
    light._state = replace(original, intensity=1000, kelvin=5000)

    async def confirm(predicate: Any, **_kwargs: Any) -> bool:
        if light._boost_output_snapshot is not None:
            light._state = original
        else:
            light._state = expected
        return predicate()

    light._async_confirm_primary_state = AsyncMock(side_effect=confirm)
    await light.async_set_gm(2)

    payloads = [call.args[0] for call in light._async_send.await_args_list]
    assert payloads == [
        telink.boost(True, 4000, 100),
        telink.boost(False, 4000, 100),
        telink.cct(2700, 400),
        telink.onoff(True),
        telink.cct(2700, 400, 2),
    ]
    assert light.state == expected
    assert light._boost_output_snapshot is None


@pytest.mark.asyncio
async def test_turn_off_while_boost_preserves_the_colour_for_the_next_turn_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OFF keeps the old look instead of retaining command 70's CCT."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    original = replace(
        light_state(is_hsi=False, intensity=400),
        kelvin=2700,
    )
    light._state = original
    light._async_send = AsyncMock()
    confirmations = 0

    await light.async_set_boost(True)
    light._state = replace(original, intensity=1000, kelvin=5500)

    async def confirm(predicate: Any, **_kwargs: Any) -> bool:
        nonlocal confirmations
        confirmations += 1
        light._state = replace(original, on=confirmations >= 3)
        return predicate()

    light._async_confirm_primary_state = AsyncMock(side_effect=confirm)
    await light.async_apply_turn_off()
    assert light.state == replace(original, on=False)
    await light.async_apply_turn_on(intensity=400, brightness_changed=False)

    payloads = [call.args[0] for call in light._async_send.await_args_list]
    assert payloads == [
        telink.boost(True, 2700, 100),
        telink.boost(False, 2700, 100),
        telink.cct(2700, 400),
        telink.onoff(False),
        telink.onoff(False),
        telink.onoff(True),
    ]
    assert light.state == original
    assert light._boost_output_snapshot is None


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_boost_same_modal_state_reasserts_each_requested_state(
    monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    """Both writes remain physical actions despite locally assumed state."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    steady = light_state(is_hsi=False)
    boost = telink.BoostState(enabled, 4300, 100)
    light._state = steady
    light._boost_state = boost
    light._async_send = AsyncMock()
    light._async_refresh_state = AsyncMock(return_value=True)

    await light.async_set_boost(enabled)

    light._async_send.assert_awaited_once_with(telink.boost(enabled, 4300, 100))
    light._async_refresh_state.assert_not_awaited()
    assert light.state is steady
    assert light.boost_state is boost


def test_unsolicited_boost_report_cannot_enable_the_modal_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A report bit the APK ignores cannot turn Home Assistant's switch on."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)

    light._on_access_message(access_message(as_report(telink.boost(True, 4300, 100))))

    assert light.boost_state == telink.BoostState(False, 4300, 100)


@pytest.mark.asyncio
async def test_boost_cct_preserves_gm_and_activation_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Changing Boost CCT preserves both modal state and cached tint."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    light._boost_state = telink.BoostState(True, 4300, 87)

    async def send(payload: bytes, retries: int = 3) -> None:
        del retries
        light._on_access_message(access_message(as_report(payload)))

    light._async_send = AsyncMock(side_effect=send)

    await light.async_set_boost_kelvin(5125)

    payload = light._async_send.await_args.args[0]
    assert telink.decode_boost(payload) == telink.BoostState(True, 5150, 87)
    assert light.boost_state == telink.BoostState(True, 5150, 87)


@pytest.mark.asyncio
async def test_ace_25c_boost_tint_preserves_cct_and_uses_app_raw_scale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Normalized -10..+10 tint maps to the Boost dialog's raw 0..200."""
    light = make_light(monkeypatch, profile=get_fixture_profile("ace_25c"))
    light._boost_state = telink.BoostState(True, 4500, 100)

    async def send(payload: bytes, retries: int = 3) -> None:
        del retries
        light._on_access_message(access_message(as_report(payload)))

    light._async_send = AsyncMock(side_effect=send)

    await light.async_set_boost_gm(-2.5)

    payload = light._async_send.await_args.args[0]
    assert telink.decode_boost(payload) == telink.BoostState(True, 4500, 75)
    assert light.boost_state == telink.BoostState(True, 4500, 75)


@pytest.mark.asyncio
async def test_boost_write_failure_does_not_mutate_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed Mesh write cannot change the locally tracked modal state."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    original = telink.BoostState(True, 4300, 100)
    light._boost_state = original
    light._async_send = AsyncMock(
        side_effect=device.AmaranConnectionError("write failed")
    )

    with pytest.raises(device.AmaranConnectionError, match="write failed"):
        await light.async_set_boost_kelvin(5000)

    assert light.boost_state == original


@pytest.mark.asyncio
async def test_fan_same_supported_mode_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A current report makes re-selecting the supported mode a no-op."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    light._fan_state = telink.decode_fan(fan_report(telink.FanMode.SMART))
    light._async_send = AsyncMock()
    light._async_refresh_optional = AsyncMock(return_value=False)
    sleep = AsyncMock()
    monkeypatch.setattr(device.asyncio, "sleep", sleep)

    await light.async_set_fan_mode("smart")

    light._async_send.assert_not_awaited()
    light._async_refresh_optional.assert_not_awaited()
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_fan_mode_retries_one_lost_confirmation_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A changed mode succeeds when the second bounded query gets its report."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    light._fan_state = telink.decode_fan(fan_report(telink.FanMode.SMART))
    light._async_send = AsyncMock()
    attempts = 0

    async def refresh(*_args: object) -> bool:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return False
        light._fan_state = telink.decode_fan(fan_report(telink.FanMode.SILENT))
        return True

    light._async_refresh_optional = AsyncMock(side_effect=refresh)
    monkeypatch.setattr(device.asyncio, "sleep", AsyncMock())

    await light.async_set_fan_mode("silent")

    light._async_send.assert_awaited_once_with(telink.fan("silent"))
    assert light._async_refresh_optional.await_count == 2
    assert light.fan_state is not None
    assert light.fan_state.mode is telink.FanMode.SILENT


@pytest.mark.asyncio
async def test_fan_mode_rejects_two_lost_confirmation_reports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A changed mode fails after exactly two unanswered status queries."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    light._fan_state = telink.decode_fan(fan_report(telink.FanMode.SMART))
    light._async_send = AsyncMock()
    light._async_refresh_optional = AsyncMock(side_effect=[False, False])
    monkeypatch.setattr(device.asyncio, "sleep", AsyncMock())

    with pytest.raises(device.AmaranConnectionError, match="did not confirm"):
        await light.async_set_fan_mode("silent")

    light._async_send.assert_awaited_once_with(telink.fan("silent"))
    assert light._async_refresh_optional.await_count == 2


@pytest.mark.asyncio
async def test_manual_fan_mode_preserves_reported_speed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Selecting Manual must not overwrite the known target with zero RPM."""
    light = make_light(monkeypatch, profile=get_fixture_profile_by_product_id("000G5"))
    light._fan_state = telink.FanState(
        telink.FanMode.SMART,
        fixture_speed=650,
        current_temperature_raw=0,
        high_temperature_raw=0,
        supported_modes=(telink.FanMode.MANUAL, telink.FanMode.SMART),
    )
    light._async_send = AsyncMock()

    async def refresh(*_args: object) -> bool:
        light._fan_state = telink.FanState(
            telink.FanMode.MANUAL,
            fixture_speed=650,
            current_temperature_raw=0,
            high_temperature_raw=0,
            supported_modes=(telink.FanMode.MANUAL, telink.FanMode.SMART),
        )
        return True

    light._async_refresh_optional = AsyncMock(side_effect=refresh)
    monkeypatch.setattr(device.asyncio, "sleep", AsyncMock())

    await light.async_set_fan_mode("manual")

    light._async_send.assert_awaited_once_with(telink.fan(telink.FanMode.MANUAL, 650))


@pytest.mark.asyncio
async def test_manual_fan_speed_requires_mode_and_fresh_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manual RPM writes clamp and succeed only after an exact fresh report."""
    light = make_light(monkeypatch, profile=get_fixture_profile_by_product_id("000G5"))
    light._fan_state = telink.FanState(
        telink.FanMode.MANUAL,
        fixture_speed=650,
        current_temperature_raw=0,
        high_temperature_raw=0,
        supported_modes=(telink.FanMode.MANUAL,),
    )
    light._async_send = AsyncMock()

    async def refresh(*_args: object) -> bool:
        light._fan_state = telink.FanState(
            telink.FanMode.MANUAL,
            fixture_speed=1000,
            current_temperature_raw=0,
            high_temperature_raw=0,
            supported_modes=(telink.FanMode.MANUAL,),
        )
        return True

    light._async_refresh_optional = AsyncMock(side_effect=refresh)
    monkeypatch.setattr(device.asyncio, "sleep", AsyncMock())

    await light.async_set_fan_speed(1000.6)

    light._async_send.assert_awaited_once_with(telink.fan(telink.FanMode.MANUAL, 1000))
    light._async_refresh_optional.assert_awaited_once_with(
        telink.fan_request(), light._fan_received
    )

    light._fan_state = telink.FanState(
        telink.FanMode.SMART,
        fixture_speed=650,
        current_temperature_raw=0,
        high_temperature_raw=0,
        supported_modes=(telink.FanMode.MANUAL, telink.FanMode.SMART),
    )
    with pytest.raises(device.AmaranConnectionError, match="select Manual"):
        await light.async_set_fan_speed(500)


@pytest.mark.asyncio
async def test_manual_fan_speed_retries_one_lost_confirmation_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A manual target succeeds when the retry receives the exact RPM."""
    light = make_light(monkeypatch, profile=get_fixture_profile_by_product_id("000G5"))
    light._fan_state = telink.FanState(
        telink.FanMode.MANUAL,
        fixture_speed=650,
        current_temperature_raw=0,
        high_temperature_raw=0,
        supported_modes=(telink.FanMode.MANUAL,),
    )
    light._async_send = AsyncMock()
    attempts = 0

    async def refresh(*_args: object) -> bool:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return False
        light._fan_state = telink.FanState(
            telink.FanMode.MANUAL,
            fixture_speed=700,
            current_temperature_raw=0,
            high_temperature_raw=0,
            supported_modes=(telink.FanMode.MANUAL,),
        )
        return True

    light._async_refresh_optional = AsyncMock(side_effect=refresh)
    monkeypatch.setattr(device.asyncio, "sleep", AsyncMock())

    await light.async_set_fan_speed(700)

    light._async_send.assert_awaited_once_with(telink.fan(telink.FanMode.MANUAL, 700))
    assert light._async_refresh_optional.await_count == 2


@pytest.mark.asyncio
async def test_manual_fan_speed_rejects_two_lost_confirmation_reports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A manual target fails after exactly two unanswered status queries."""
    light = make_light(monkeypatch, profile=get_fixture_profile_by_product_id("000G5"))
    light._fan_state = telink.FanState(
        telink.FanMode.MANUAL,
        fixture_speed=650,
        current_temperature_raw=0,
        high_temperature_raw=0,
        supported_modes=(telink.FanMode.MANUAL,),
    )
    light._async_send = AsyncMock()
    light._async_refresh_optional = AsyncMock(side_effect=[False, False])
    monkeypatch.setattr(device.asyncio, "sleep", AsyncMock())

    with pytest.raises(device.AmaranConnectionError, match="did not confirm"):
        await light.async_set_fan_speed(700)

    light._async_send.assert_awaited_once_with(telink.fan(telink.FanMode.MANUAL, 700))
    assert light._async_refresh_optional.await_count == 2


@pytest.mark.asyncio
async def test_manual_fan_speed_rejects_stale_or_wrong_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale cached RPM cannot confirm an unanswered target-speed write."""
    light = make_light(monkeypatch, profile=get_fixture_profile_by_product_id("000G5"))
    light._fan_state = telink.FanState(
        telink.FanMode.MANUAL,
        fixture_speed=650,
        current_temperature_raw=0,
        high_temperature_raw=0,
        supported_modes=(telink.FanMode.MANUAL,),
    )
    light._async_send = AsyncMock()
    light._async_refresh_optional = AsyncMock(return_value=True)
    monkeypatch.setattr(device.asyncio, "sleep", AsyncMock())

    with pytest.raises(device.AmaranConnectionError, match="did not confirm"):
        await light.async_set_fan_speed(700)

    assert light._async_refresh_optional.await_count == 2


def test_write_form_pages_cannot_confirm_their_own_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Incoming set-form echoes are ignored until a real report arrives."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)

    for payload in (
        telink.effect(telink.SystemEffect.TV),
        telink.boost(True, 4300),
        telink.fan(telink.FanMode.SMART),
    ):
        light._on_access_message(access_message(payload))

    assert light.effect_state is None
    assert light.boost_state is None
    assert light.fan_state is None
    assert not light._state_received.is_set()
    assert not light._boost_received.is_set()
    assert not light._fan_received.is_set()


@pytest.mark.asyncio
async def test_turn_off_still_powers_down_when_boost_exit_write_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed Boost write must never block the safety-critical OFF packet."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    light._boost_state = telink.BoostState(True, 4300, 100)
    light._async_set_boost_unlocked = AsyncMock(
        side_effect=device.AmaranConnectionError("Boost write failed")
    )
    light.async_turn_off = AsyncMock()
    light._async_confirm_primary_state = AsyncMock(return_value=True)

    with pytest.raises(device.AmaranConnectionError, match="was turned off"):
        await light.async_apply_turn_off()

    light.async_turn_off.assert_awaited_once_with()
    light._async_confirm_primary_state.assert_awaited_once()


def test_boost_is_parallel_to_primary_light_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Boost is a modal preview and never replaces primary CCT/FX state."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    light._proxy = Mock()

    steady_payload = as_report(telink.cct(4300, 250), on=True)
    light._on_access_message(access_message(steady_payload))
    assert light.boost_state == telink.BoostState(False, 4300, 100)
    assert light.available

    primary = light.state
    light._state_received.clear()
    light._on_access_message(access_message(as_report(telink.boost(True, 5000))))
    assert light.boost_state == telink.BoostState(False, 5000, 100)
    assert light.state == primary
    assert not light._state_received.is_set()
    assert light.available

    light._state = None
    assert not light.available


def test_disconnect_invalidates_every_report_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No entity remains available from stale diagnostic data after a link loss."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    light._state = light_state(is_hsi=False)
    light._effect_state = telink.decode_effect(telink.effect(telink.SystemEffect.FIRE))
    light._boost_state = telink.decode_boost(telink.boost(True, 4300))
    light._fan_state = telink.decode_fan(telink.fan(telink.FanMode.SMART))
    light._power_state = telink.decode_power(bytes.fromhex("f80000779ba43800000a"))
    light._version_state = telink.decode_version(bytes.fromhex("a3b7cca7f40c6e62a900"))
    light._version2_state = telink.decode_version2(
        bytes.fromhex("4d0000d08cc2cb6ad525")
    )
    light._high_speed_state = highspeed.decode_high_speed(
        highspeed.build_high_speed(True)
    )

    light._clear_report_state()

    assert light.state is None
    assert light.effect_state is None
    assert light.boost_state is None
    assert light.fan_state is None
    assert light.power_state is None
    assert light.version_state is None
    assert light.version2_state is None
    assert light.high_speed_state is None


def test_disconnect_keeps_pixel_generation_monotonic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A report after reconnect must be newer than an operation's old baseline."""
    light = make_light(monkeypatch, profile=get_fixture_profile_by_product_id("000F5"))
    report = as_report(pixelfx.effect(pixelfx.PixelEffect.RAINBOW)[0])
    for _ in range(3):
        light._on_access_message(access_message(report))
    baseline = light._pixel_report_generation

    light._clear_report_state()
    light._on_access_message(access_message(report))

    assert light._pixel_report_generation > baseline
    assert light._pixel_page_generations[(2, 0)] > baseline
