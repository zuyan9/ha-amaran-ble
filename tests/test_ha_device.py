"""Home Assistant-runtime tests for crash-safe device persistence helpers."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

pytest.importorskip("homeassistant")

from custom_components.amaran_ble import device
from custom_components.amaran_ble.amaranble import telink


class FakeStore:
    """Minimal async Store stand-in for merge tests."""

    def __init__(self, value: dict[str, int] | None) -> None:
        self.value = value

    async def async_load(self) -> dict[str, int] | None:
        return self.value


def make_light(monkeypatch: pytest.MonkeyPatch) -> device.AmaranLight:
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
    )


def light_state(*, is_hsi: bool, gm: int = 0) -> telink.LightState:
    """Return a representative fixture status."""
    return telink.LightState(
        on=True,
        is_hsi=is_hsi,
        intensity=640,
        kelvin=4300 if not is_hsi else 0,
        gm=gm,
        hue=120 if is_hsi else 0,
        saturation=75 if is_hsi else 0,
    )


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
async def test_not_provisioned_reconnect_backs_off_and_can_recover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale provisioning advert must not permanently strand a healthy light."""
    light = make_light(monkeypatch)
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


@pytest.mark.asyncio
async def test_gm_in_hsi_mode_updates_cache_only_with_half_up_rounding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HSI has no G/M wire field, so retain the rounded preference locally."""
    light = make_light(monkeypatch)
    light._state = light_state(is_hsi=True)
    light.async_set_cct = AsyncMock()
    light._async_refresh_state = AsyncMock()
    listener = Mock()
    light.add_listener(listener)

    await light.async_set_gm(2.5)
    assert light.preferred_gm == 3
    await light.async_set_gm(-2.5)
    assert light.preferred_gm == -2

    light.async_set_cct.assert_not_awaited()
    light._async_refresh_state.assert_not_awaited()
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
    light._async_refresh_state = AsyncMock(return_value=True)

    await light.async_set_gm(2.5)

    light.async_set_cct.assert_awaited_once_with(4300, 640, 3)
    light._async_refresh_state.assert_awaited_once_with()
    assert light.preferred_gm == 3
