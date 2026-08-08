"""Home Assistant-runtime tests for crash-safe device persistence helpers."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

pytest.importorskip("homeassistant")

from custom_components.amaran_ble import device
from custom_components.amaran_ble.amaranble import telink
from custom_components.amaran_ble.amaranble.proxy import AccessMessage
from custom_components.amaran_ble.profiles import ACE_25X_PROFILE, GENERIC_PROFILE


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
    gm: int = 0,
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


def test_typed_reports_update_only_their_own_cache_and_waiter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Optional report pages cannot falsely satisfy a primary state request."""
    light = make_light(monkeypatch, profile=ACE_25X_PROFILE)
    light._proxy = Mock()
    listener = Mock()
    light.add_listener(listener)

    version = bytes.fromhex("a3b7cca7f40c6e62a900")
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

    light._on_access_message(access_message(power))
    assert light.power_state is not None
    assert light.power_state.runtime_minutes == 3000
    assert light._power_received.is_set()
    assert not light._state_received.is_set()

    light._on_access_message(access_message(effect))
    assert light.effect_state == telink.decode_effect(effect)
    assert light._state_received.is_set()
    assert light.available
    assert listener.call_count == 3


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
    ]

    for payload in payloads:
        light._on_access_message(access_message(payload))

    assert light.effect_state is None
    assert light.boost_state is None
    assert light.fan_state is None
    assert light.power_state is None
    assert light.version_state is None
    assert not light._state_received.is_set()
    assert not light._boost_received.is_set()
    assert not light._fan_received.is_set()
    assert not light._power_received.is_set()
    assert not light._version_received.is_set()
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

    light._clear_report_state()

    assert light.state is None
    assert light.effect_state is None
    assert light.boost_state is None
    assert light.fan_state is None
    assert light.power_state is None
    assert light.version_state is None
