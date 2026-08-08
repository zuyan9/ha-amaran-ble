"""Home Assistant-runtime tests for crash-safe device persistence helpers."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

pytest.importorskip("homeassistant")

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
    monkeypatch: pytest.MonkeyPatch, *, profile=GENERIC_PROFILE
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
        device.bluetooth, "async_ble_device_from_address", Mock(return_value=Mock())
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
                intensity=321,
                frequency=6,
                speed=7,
                trigger=2,
                kelvin=4300,
            )
            light._on_access_message(access_message(as_report(wrong)))

    light._async_send = AsyncMock(side_effect=send)

    with pytest.raises(device.AmaranConnectionError, match="effect rate"):
        await light.async_set_effect_frequency(6)

    assert status_requests == 3


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
    """A firmware-chosen speed cannot turn a successful FX switch into an error."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    light._state = light_state(is_hsi=False, intensity=10)

    async def send(payload: bytes, retries: int = 3) -> None:
        del retries
        if payload != telink.status_request():
            return
        normalized = telink.effect(
            telink.SystemEffect.PULSING,
            intensity=10,
            frequency=5,
            speed=1,
            trigger=2,
            kelvin=4300,
        )
        light._on_access_message(access_message(as_report(normalized)))

    light._async_send = AsyncMock(side_effect=send)

    await light.async_apply_effect(telink.SystemEffect.PULSING, intensity=10)

    assert light.effect_state is not None
    assert light.effect_state.effect is telink.SystemEffect.PULSING
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
    """A default-safe command-34 effect is selectable through the light API."""
    profile = get_fixture_profile_by_product_id("000G5")
    light = make_light(monkeypatch, profile=profile)
    light._proxy = Mock()
    light._state = light_state(is_hsi=True)
    latest: bytes | None = None

    async def send(payload: bytes, retries: int = 3) -> None:
        nonlocal latest
        del retries
        if payload[9] & 0x7F == systemfx2.CMD_SYSTEM_EFFECT_2:
            latest = as_report(payload)
            light._on_access_message(access_message(latest))
        elif payload == telink.status_request() and latest is not None:
            light._on_access_message(access_message(latest))

    light._async_send = AsyncMock(side_effect=send)

    await light.async_apply_effect("Lightning II", intensity=321)

    assert light.effect2_state is not None
    assert light.effect2_state.effect is systemfx2.SystemEffect2.LIGHTNING_II
    assert light.effect2_state.intensity == 321
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

    light._async_send = AsyncMock(side_effect=send)

    await light.async_set_boost(True)
    await light.async_set_boost(False)
    await light.async_set_fan_mode("silent")

    assert light.boost_state is not None and not light.boost_state.enabled
    assert light.boost_state.kelvin == 4300
    assert light.fan_state is not None
    assert light.fan_state.mode is telink.FanMode.SILENT
    assert light._async_refresh_state.await_count == 2


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
async def test_fan_same_mode_still_requires_fresh_query_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale cached mode cannot make an unanswered fan write succeed."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    light._fan_state = telink.decode_fan(fan_report(telink.FanMode.SMART))
    light._async_send = AsyncMock()
    light._async_refresh_optional = AsyncMock(return_value=False)
    monkeypatch.setattr(device.asyncio, "sleep", AsyncMock())

    with pytest.raises(device.AmaranConnectionError, match="did not confirm"):
        await light.async_set_fan_mode("smart")

    light._async_send.assert_awaited_once_with(telink.fan("smart"))
    light._async_refresh_optional.assert_awaited_once_with(
        telink.fan_request(), light._fan_received
    )


@pytest.mark.asyncio
async def test_manual_fan_mode_preserves_reported_speed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Selecting Manual must not overwrite the known target with zero RPM."""
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
