"""Connection management for a single provisioned amaran fixture.

Each config entry owns a private one-node mesh and holds a persistent GATT
connection to its fixture, which is what lets the fixture push state changes
(including ones made at the light's own control knob) straight into Home
Assistant.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import replace

from bleak import BleakClient
from bleak.exc import BleakError
from bleak_retry_connector import establish_connection
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .amaranble import network, telink
from .amaranble.config_client import ConfigClient
from .amaranble.gatt import MESH_PROVISIONING_SERVICE, MESH_PROXY_SERVICE
from .amaranble.proxy import AccessMessage, ProxyClient, ProxyError
from .amaranble.sequence import SequenceExhaustedError, SequenceReservation
from .const import (
    DOMAIN,
    INITIAL_POLL_INTERVAL,
    POLL_INTERVAL,
    RECONNECT_MAX_DELAY,
    RECONNECT_MIN_DELAY,
    SEQUENCE_CHECKPOINT,
)
from .profiles import FixtureProfile

_LOGGER = logging.getLogger(__name__)

DISCONNECT_TIMEOUT = 5.0
MAX_MISSED_POLLS = 3
OPTIONAL_REPORT_TIMEOUT = 0.8
FAN_APPLY_SETTLE = 0.2


def _sequence_store(hass: HomeAssistant, sequence_store_id: str) -> Store[dict]:
    """Build the private atomic store used for replay-protection state."""
    return Store(
        hass,
        1,
        f"{DOMAIN}.{sequence_store_id}",
        private=True,
        atomic_writes=True,
    )


async def _async_verified_sequence_save(
    hass: HomeAssistant,
    sequence_store_id: str,
    store: Store[dict],
    data: dict[str, int],
) -> None:
    """Persist and read back a high-water mark before it is trusted.

    Home Assistant's Store logs filesystem write failures instead of raising
    them. A fresh Store read makes a disk-full or failed atomic replace visible
    to the sequence allocator, which then refuses to transmit from that block.
    """
    await store.async_save(data)
    persisted = await _sequence_store(hass, sequence_store_id).async_load()
    if persisted != data:
        raise OSError("Bluetooth Mesh sequence reservation was not persisted")


def _sequence_high_water(stored: dict | None) -> int | None:
    """Return the next safe sequence represented by one store generation."""
    if not stored:
        return None
    if "reserved_until" in stored:
        return int(stored["reserved_until"])
    if "sequence" in stored:
        # The oldest store format recorded an in-memory sequence rather than
        # an exclusive reservation, so retain its one-block migration skip.
        return int(stored["sequence"]) + SEQUENCE_CHECKPOINT
    return None


async def _async_load_sequence_stores(
    stable_store: Store[dict], compatibility_store: Store[dict] | None
) -> dict[str, int]:
    """Merge stable and rollback-compatible stores conservatively."""
    stable = await stable_store.async_load()
    compatibility = (
        await compatibility_store.async_load() if compatibility_store else None
    )
    high_waters = [
        high_water
        for stored in (stable, compatibility)
        if (high_water := _sequence_high_water(stored)) is not None
    ]
    if not high_waters:
        return {}
    high_water = max(high_waters)
    return {"reserved_until": high_water, "sequence": high_water}


async def _async_save_sequence_stores(
    hass: HomeAssistant,
    stable_store_id: str,
    stable_store: Store[dict],
    compatibility_store_id: str,
    compatibility_store: Store[dict] | None,
    data: dict[str, int],
) -> None:
    """Write the high-water mark stable-first, then for rollback safety."""
    await _async_verified_sequence_save(hass, stable_store_id, stable_store, data)
    if compatibility_store is not None:
        await _async_verified_sequence_save(
            hass,
            compatibility_store_id,
            compatibility_store,
            data,
        )


class AmaranConnectionError(Exception):
    """The fixture could not be reached."""


class AmaranNotProvisionedError(Exception):
    """The fixture no longer belongs to our mesh and must be re-provisioned."""


class NodeConfigurationError(Exception):
    """Post-provision model configuration failed after consuming sequences."""

    def __init__(self, message: str, sequence: int) -> None:
        super().__init__(message)
        self.sequence = sequence


class AmaranLight:
    """Owns the BLE link, the mesh session and the cached light state."""

    def __init__(
        self,
        hass: HomeAssistant,
        sequence_store_id: str,
        address: str,
        name: str,
        *,
        compatibility_sequence_store_id: str | None = None,
        net_key: bytes,
        app_key: bytes,
        device_key: bytes,
        unicast_address: int,
        local_address: int,
        iv_index: int,
        initial_sequence: int = 0,
        profile: FixtureProfile,
    ) -> None:
        self.hass = hass
        self.address = address
        self.name = name
        self.profile = profile

        self._sequence_store_id = sequence_store_id
        self._compatibility_sequence_store_id = (
            compatibility_sequence_store_id or sequence_store_id
        )
        self._net_key = net_key
        self._app_key = app_key
        self._device_key = device_key
        self._unicast_address = unicast_address
        self._local_address = local_address
        self._iv_index = iv_index
        self._initial_sequence = initial_sequence

        self._store = _sequence_store(hass, sequence_store_id)
        self._compatibility_store = (
            None
            if self._compatibility_sequence_store_id == sequence_store_id
            else _sequence_store(hass, self._compatibility_sequence_store_id)
        )
        self._sequence = 0
        self._sequence_reservation: SequenceReservation | None = None
        # These receive-side replay records belong to the NetKey/IV Index, not
        # to one transient GATT connection. Retain them across BLE reconnects.
        self._inbound_replay_list: dict[int, int] = {}
        self._segment_reassembler = network.SegmentReassembler()

        self._client: BleakClient | None = None
        self._proxy: ProxyClient | None = None
        self._connect_lock = asyncio.Lock()
        # Light and number entities have separate Home Assistant semaphores.
        # Keep their multi-packet operations (parameters, power, status) from
        # interleaving at the fixture.
        self._operation_lock = asyncio.Lock()
        self._closing = False
        self._reconnect_task: asyncio.Task | None = None
        self._poll_task: asyncio.Task | None = None
        self._proxy_cleanup_tasks: set[asyncio.Task] = set()
        self._reconnect_delay = RECONNECT_MIN_DELAY
        self._missed_polls = 0

        # Keep the last steady state while a built-in effect is active. That
        # lets selecting HA's ``off`` effect return to the user's previous CCT
        # look instead of inventing a new one.
        self._state: telink.LightState | None = None
        self._effect_state: telink.EffectState | None = None
        self._boost_state: telink.BoostState | None = None
        self._fan_state: telink.FanState | None = None
        self._power_state: telink.PowerState | None = None
        self._version_state: telink.VersionState | None = None
        # HSI reports do not carry G/M, so retain the last CCT tint instead of
        # treating every switch to colour mode as a reset to neutral.
        self._preferred_gm = 0
        self._state_received = asyncio.Event()
        self._boost_received = asyncio.Event()
        self._fan_received = asyncio.Event()
        self._power_received = asyncio.Event()
        self._version_received = asyncio.Event()
        self._listeners: list[Callable[[], None]] = []

    # ─── Public surface ──────────────────────────────────────────────────────

    @property
    def state(self) -> telink.LightState | None:
        return self._state

    @property
    def effect_state(self) -> telink.EffectState | None:
        return self._effect_state

    @property
    def boost_state(self) -> telink.BoostState | None:
        return self._boost_state

    @property
    def fan_state(self) -> telink.FanState | None:
        return self._fan_state

    @property
    def power_state(self) -> telink.PowerState | None:
        return self._power_state

    @property
    def version_state(self) -> telink.VersionState | None:
        return self._version_state

    @property
    def preferred_gm(self) -> int:
        """Return the last known or requested green/magenta adjustment."""
        return self._preferred_gm

    @property
    def available(self) -> bool:
        return self._proxy is not None and (
            self._state is not None or self._effect_state is not None
        )

    @property
    def connected(self) -> bool:
        """Return whether the current BLE Mesh proxy link is usable."""
        return self._proxy is not None

    @property
    def available_fan_modes(self) -> tuple[str, ...]:
        """Return profile-approved modes confirmed by the fixture report."""
        state = self._fan_state
        if state is None:
            return ()
        supported = {mode.value for mode in state.supported_modes}
        return tuple(mode for mode in self.profile.fan_modes if mode in supported)

    @property
    def effect_frequency_available(self) -> bool:
        """Whether the active built-in effect has a rate parameter."""
        return self._effect_state is not None and self._effect_state.on

    @property
    def effect_color_temperature_available(self) -> bool:
        """Whether the active effect carries an adjustable CCT field."""
        return (
            self._effect_state is not None
            and self._effect_state.on
            and self._effect_state.kelvin is not None
        )

    @property
    def effect_variant_options(self) -> tuple[str, ...]:
        """Return the active effect's app-defined colour presets."""
        state = self._effect_state
        if state is None or not state.on:
            return ()
        if state.effect in {telink.SystemEffect.TV, telink.SystemEffect.FIRE}:
            return ("warmer", "natural", "cooler")
        if state.effect is telink.SystemEffect.FIREWORKS:
            return ("warmer", "cooler", "multi")
        return ()

    @callback
    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    async def async_start(self) -> None:
        """Load persisted state and make the first connection attempt."""
        stored = await _async_load_sequence_stores(
            self._store, self._compatibility_store
        )
        try:
            self._sequence_reservation = SequenceReservation.create(
                stored,
                self._async_save_sequence,
                block_size=SEQUENCE_CHECKPOINT,
                minimum_sequence=self._initial_sequence,
            )
        except SequenceExhaustedError as err:
            raise AmaranConnectionError(str(err)) from err
        self._sequence = self._sequence_reservation.next_sequence

        await self._async_connect()
        self._poll_task = self.hass.async_create_background_task(
            self._poll_loop(), f"{DOMAIN} poll {self.address}"
        )

    async def async_stop(self) -> None:
        self._closing = True
        tasks = [
            task for task in (self._reconnect_task, self._poll_task) if task is not None
        ]
        try:
            for task in tasks:
                task.cancel()
            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for result in results:
                    if isinstance(result, BaseException) and not isinstance(
                        result, asyncio.CancelledError
                    ):
                        _LOGGER.debug(
                            "background task for %s stopped with an error: %s",
                            self.address,
                            result,
                        )
        finally:
            self._reconnect_task = None
            self._poll_task = None
            # A service call can still be finishing its first connection while
            # Home Assistant unloads the entry. Wait for that setup to unwind
            # before clearing the client so it cannot install a connection
            # after teardown has completed.
            async with self._connect_lock:
                await self._async_disconnect()
            cleanup_tasks = list(self._proxy_cleanup_tasks)
            if cleanup_tasks:
                await asyncio.gather(*cleanup_tasks, return_exceptions=True)

    async def async_turn_on(self) -> None:
        await self._async_send(telink.onoff(True))

    async def async_turn_off(self) -> None:
        await self._async_send(telink.onoff(False))

    async def async_set_brightness(self, intensity: int) -> None:
        await self._async_send(telink.brightness(intensity))

    async def async_set_cct(self, kelvin: int, intensity: int, gm: float = 0) -> None:
        await self._async_send(telink.cct(kelvin, intensity, gm))

    async def async_set_hsi(
        self, hue: float, saturation: float, intensity: int
    ) -> None:
        await self._async_send(telink.hsi(hue, saturation, intensity))

    def _effect_payload(
        self,
        state: telink.EffectState,
        *,
        intensity: float | None = None,
        frequency: float | None = None,
        kelvin: float | None = None,
        variant: float | None = None,
    ) -> bytes:
        """Rebuild the active effect's complete state packet."""
        return telink.effect(
            state.effect,
            intensity=state.intensity if intensity is None else intensity,
            frequency=state.frequency if frequency is None else frequency,
            speed=state.speed or 5,
            trigger=state.trigger,
            kelvin=(
                state.kelvin or self._default_effect_kelvin()
                if kelvin is None
                else kelvin
            ),
            variant=state.variant if variant is None else variant,
        )

    def _default_effect_kelvin(self) -> int:
        """Choose an in-range CCT for effects that carry a white point."""
        if self._state is not None and not self._state.is_hsi:
            return min(
                max(self._state.kelvin, self.profile.min_kelvin),
                self.profile.max_kelvin,
            )
        return (self.profile.min_kelvin + self.profile.max_kelvin) // 2

    async def async_apply_effect(
        self, effect: telink.SystemEffect | str, *, intensity: int | None = None
    ) -> None:
        """Select a built-in effect as part of a Home Assistant turn-on."""
        if not self.profile.supports_effects:
            raise AmaranConnectionError(f"effects are not enabled for {self.name}")
        try:
            selected = telink.SystemEffect(effect)
        except ValueError as err:
            raise AmaranConnectionError(
                f"{effect!r} is not a supported effect for {self.name}"
            ) from err
        if selected.value not in self.profile.effects:
            raise AmaranConnectionError(
                f"{selected.value!r} is not a supported effect for {self.name}"
            )
        async with self._operation_lock:
            if selected is telink.SystemEffect.OFF:
                await self._async_exit_effect(intensity=intensity)
                return

            current = self._effect_state
            if self._boost_state is not None and self._boost_state.enabled:
                await self._async_set_boost_unlocked(False)
            output_was_on = (
                current.on
                if current is not None
                else self._state is not None and self._state.on
            )
            preserving_active_effect = (
                current is not None and current.effect is selected
            )
            if preserving_active_effect:
                expected_intensity = (
                    current.intensity if intensity is None else intensity
                )
                payload = self._effect_payload(current, intensity=expected_intensity)
            else:
                expected_intensity = (
                    intensity
                    if intensity is not None
                    else (
                        current.intensity
                        if current is not None and current.intensity > 0
                        else (
                            self._state.intensity
                            if self._state is not None and self._state.intensity > 0
                            else 180
                        )
                    )
                )
                payload = telink.effect(
                    selected,
                    intensity=expected_intensity,
                    frequency=5,
                    speed=5,
                    kelvin=self._default_effect_kelvin(),
                )
            expected_effect = telink.decode_effect(payload)
            assert expected_effect is not None
            await self._async_send(payload)
            # As with CCT/HSI, an effect parameter packet does not reliably
            # wake a sleeping Ace. Power it on only when this is a turn-on.
            if not output_was_on:
                await self.async_turn_on()

            def effect_matches() -> bool:
                report = self._effect_state
                if preserving_active_effect:
                    # Brightness changes on the currently selected effect must
                    # preserve every reported field we sent back to the light.
                    return report == expected_effect
                # Firmware chooses and normalizes opaque defaults (notably
                # speed/trigger) when a different effect is selected. Confirm
                # the user-visible transition without pretending those defaults
                # are fixed across firmware versions.
                return (
                    report is not None
                    and report.on
                    and report.effect is selected
                    and report.intensity == expected_intensity
                )

            if not await self._async_confirm_primary_state(effect_matches):
                raise AmaranConnectionError(
                    f"{self.name} did not confirm effect {selected.value}"
                )

    async def _async_exit_effect(self, *, intensity: int | None = None) -> None:
        """Leave system-FX mode and restore the last steady light look."""
        current = self._effect_state
        steady = self._state
        if steady is not None:
            target_intensity = steady.intensity if intensity is None else intensity
            if steady.is_hsi:
                await self.async_set_hsi(
                    steady.hue, steady.saturation, target_intensity
                )
            else:
                await self.async_set_cct(
                    steady.kelvin, target_intensity, self._preferred_gm
                )
        elif current is not None:
            target_intensity = intensity if intensity is not None else current.intensity
            await self.async_set_cct(
                self._default_effect_kelvin(),
                target_intensity or 180,
                self._preferred_gm,
            )
        else:
            target_intensity = intensity
            if target_intensity is not None:
                await self.async_set_brightness(target_intensity)

        # This method is reached through ``light.turn_on(effect="off")``.
        # A CCT/brightness packet does not reliably wake a sleeping fixture.
        if current is not None or steady is None or not steady.on:
            await self.async_turn_on()
        if not await self._async_confirm_primary_state(
            lambda: (
                self._effect_state is None
                and self._state is not None
                and self._state.on
                and (intensity is None or self._state.intensity == intensity)
            )
        ):
            raise AmaranConnectionError(
                f"{self.name} did not confirm leaving its effect"
            )

    async def async_set_effect_frequency(self, frequency: float) -> None:
        """Set the active effect rate, including the APK's value 11=Random."""
        async with self._operation_lock:
            state = self._effect_state
            if state is None or not state.on:
                raise AmaranConnectionError(
                    f"{self.name} is not running an active effect"
                )
            maximum = (
                10
                if state.effect
                in {telink.SystemEffect.FIRE, telink.SystemEffect.EXPLOSION}
                else 11
            )
            target = max(1, min(maximum, math.floor(frequency + 0.5)))
            payload = self._effect_payload(state, frequency=target)
            expected = telink.decode_effect(payload)
            assert expected is not None
            await self._async_send(payload)
            if not await self._async_confirm_primary_state(
                lambda: self._effect_state == expected
            ):
                raise AmaranConnectionError(
                    f"{self.name} did not confirm its effect rate"
                )

    async def async_set_effect_kelvin(self, kelvin: float) -> None:
        """Set the CCT field of an active Ace effect in the app's 50 K steps."""
        async with self._operation_lock:
            state = self._effect_state
            if state is None or not state.on or state.kelvin is None:
                raise AmaranConnectionError(
                    f"{self.name} is not running an active CCT-adjustable effect"
                )
            target = self.profile.min_kelvin + 50 * math.floor(
                (kelvin - self.profile.min_kelvin) / 50 + 0.5
            )
            target = min(max(target, self.profile.min_kelvin), self.profile.max_kelvin)
            payload = self._effect_payload(state, kelvin=target)
            expected = telink.decode_effect(payload)
            assert expected is not None
            await self._async_send(payload)
            if not await self._async_confirm_primary_state(
                lambda: self._effect_state == expected
            ):
                raise AmaranConnectionError(
                    f"{self.name} did not confirm its effect colour temperature"
                )

    async def async_set_effect_variant(self, option: str) -> None:
        """Set the app-defined colour preset of TV, Fire, or Fireworks."""
        async with self._operation_lock:
            state = self._effect_state
            options = self.effect_variant_options
            if state is None or not state.on or option not in options:
                raise AmaranConnectionError(
                    f"{option!r} is not available for the active effect"
                )
            target = options.index(option)
            payload = self._effect_payload(state, variant=target)
            expected = telink.decode_effect(payload)
            assert expected is not None
            await self._async_send(payload)
            if not await self._async_confirm_primary_state(
                lambda: self._effect_state == expected
            ):
                raise AmaranConnectionError(
                    f"{self.name} did not confirm its effect colour preset"
                )

    async def async_set_boost(self, enabled: bool) -> None:
        """Enter or leave the Ace Boost modal session after a Mesh write."""
        if not self.profile.supports_boost:
            raise AmaranConnectionError(f"Boost is not enabled for {self.name}")
        async with self._operation_lock:
            await self._async_set_boost_unlocked(enabled)
            await self._async_refresh_state()

    async def _async_set_boost_unlocked(
        self, enabled: bool, *, kelvin: int | None = None
    ) -> None:
        """Set Boost while the caller owns the device operation lock."""
        minimum = self.profile.boost_min_kelvin or self.profile.min_kelvin
        maximum = self.profile.boost_max_kelvin or self.profile.max_kelvin
        if kelvin is None:
            kelvin = (
                self._boost_state.kelvin
                if self._boost_state is not None
                else self._default_effect_kelvin()
            )
        kelvin = min(max(kelvin, minimum), maximum)
        gm = self._boost_state.gm if self._boost_state is not None else 100
        # The official app treats command 70 as a write-only modal session: it
        # sends state=1 while its Boost dialog is open, state=0 on dismiss, and
        # does not wait for or trust a state report. Ace 25x hardware likewise
        # sends no timely command-70 response. Track the session after the Mesh
        # write succeeds while still accepting any asynchronous parameter report.
        self._boost_received.clear()
        await self._async_send(telink.boost(enabled, kelvin, gm))
        reported = self._boost_state if self._boost_received.is_set() else None
        updated = telink.BoostState(
            enabled,
            reported.kelvin if reported is not None else kelvin,
            reported.gm if reported is not None else gm,
        )
        if updated != self._boost_state:
            self._boost_state = updated
            self._notify_listeners()

    async def async_set_boost_kelvin(self, kelvin: float) -> None:
        """Adjust Boost CCT without changing whether Boost is active."""
        if not self.profile.supports_boost:
            raise AmaranConnectionError(f"Boost is not enabled for {self.name}")
        async with self._operation_lock:
            state = self._boost_state
            if state is None or not state.enabled:
                raise AmaranConnectionError(
                    f"enable Boost on {self.name} before changing its colour temperature"
                )
            minimum = self.profile.boost_min_kelvin or self.profile.min_kelvin
            maximum = self.profile.boost_max_kelvin or self.profile.max_kelvin
            target = minimum + 50 * math.floor((kelvin - minimum) / 50 + 0.5)
            target = min(max(target, minimum), maximum)
            await self._async_set_boost_unlocked(state.enabled, kelvin=target)

    async def async_set_fan_mode(self, mode: str) -> None:
        """Set a fixture-confirmed fan mode, then query its full report."""
        if mode not in self.profile.fan_modes:
            raise AmaranConnectionError(f"{mode} is not supported by {self.name}")
        selected = telink.FanMode(mode)
        async with self._operation_lock:
            needs_capability_report = (
                self._fan_state is None
                or selected not in self._fan_state.supported_modes
            )
            if needs_capability_report and not await self._async_refresh_optional(
                telink.fan_request(), self._fan_received
            ):
                raise AmaranConnectionError(
                    f"{self.name} did not report its supported fan modes"
                )
            if (
                self._fan_state is None
                or selected not in self._fan_state.supported_modes
            ):
                raise AmaranConnectionError(f"{mode} is not supported by {self.name}")

            self._fan_received.clear()
            await self._async_send(telink.fan(selected))
            # The official app always queries after a fan write. A set packet
            # echo lacks capability and temperature fields and is not enough
            # to confirm that the fixture applied the requested mode.
            await asyncio.sleep(FAN_APPLY_SETTLE)
            confirmed = await self._async_refresh_optional(
                telink.fan_request(), self._fan_received
            )
            if (
                not confirmed
                or self._fan_state is None
                or self._fan_state.mode is not selected
                or selected not in self._fan_state.supported_modes
            ):
                raise AmaranConnectionError(
                    f"{self.name} did not confirm fan mode {mode}"
                )

    async def async_apply_turn_on(
        self,
        *,
        intensity: int,
        brightness_changed: bool,
        kelvin: int | None = None,
        hs_color: tuple[float, float] | None = None,
    ) -> None:
        """Apply light parameters, power, and state refresh as one operation."""
        async with self._operation_lock:
            state = self._state
            effect_state = self._effect_state
            expected_gm = self._preferred_gm
            boost_active = self._boost_state is not None and self._boost_state.enabled
            if boost_active and (
                brightness_changed or hs_color is not None or kelvin is not None
            ):
                await self._async_set_boost_unlocked(False)
                boost_active = False
            if effect_state is not None and hs_color is None and kelvin is None:
                expected_intensity = (
                    intensity if brightness_changed else effect_state.intensity
                )
                expected_effect = effect_state
                if brightness_changed:
                    payload = self._effect_payload(
                        effect_state, intensity=expected_intensity
                    )
                    expected_effect = telink.decode_effect(payload)
                    assert expected_effect is not None
                    await self._async_send(payload)
                if not effect_state.on:
                    await self.async_turn_on()
                    expected_effect = replace(expected_effect, on=True)
                if not await self._async_confirm_primary_state(
                    lambda: self._effect_state == expected_effect
                ):
                    raise AmaranConnectionError(
                        f"{self.name} did not confirm its effect state"
                    )
                return

            if hs_color is not None:
                await self.async_set_hsi(hs_color[0], hs_color[1], intensity)
            elif kelvin is not None:
                await self.async_set_cct(kelvin, intensity, expected_gm)
            elif brightness_changed:
                await self.async_set_brightness(intensity)

            # Parameter messages do not wake a sleeping fixture. Power on
            # last so it never flashes at the previous settings.
            if effect_state is not None or state is None or not state.on:
                await self.async_turn_on()

            def steady_state_matches() -> bool:
                current = self._state
                if self._effect_state is not None or current is None or not current.on:
                    return False
                if current.intensity != intensity and (
                    brightness_changed or hs_color is not None or kelvin is not None
                ):
                    return False
                if hs_color is not None:
                    return (
                        current.is_hsi
                        and current.hue == math.floor(hs_color[0] + 0.5)
                        and current.saturation == math.floor(hs_color[1] + 0.5)
                    )
                if kelvin is not None:
                    expected_kelvin = int((kelvin + 5) // 10) * 10
                    return (
                        not current.is_hsi
                        and current.kelvin == expected_kelvin
                        and current.gm == expected_gm
                    )
                return True

            if not await self._async_confirm_primary_state(steady_state_matches):
                raise AmaranConnectionError(
                    f"{self.name} did not confirm its requested light state"
                )

    async def async_apply_turn_off(self) -> None:
        """Power down and refresh without interleaving another entity command."""
        async with self._operation_lock:
            boost_error: AmaranConnectionError | None = None
            if self._boost_state is not None and self._boost_state.enabled:
                try:
                    await self._async_set_boost_unlocked(False)
                except AmaranConnectionError as err:
                    # Safety beats perfect mode bookkeeping: an unavailable
                    # Boost exit write must never prevent the user's OFF.
                    boost_error = err
            await self.async_turn_off()
            power_off_confirmed = await self._async_confirm_primary_state(
                lambda: (
                    (self._effect_state is not None and not self._effect_state.on)
                    or (
                        self._effect_state is None
                        and self._state is not None
                        and not self._state.on
                    )
                )
            )
            if not power_off_confirmed:
                raise AmaranConnectionError(
                    f"{self.name} received the off command but did not confirm power off"
                )
            if boost_error is not None:
                raise AmaranConnectionError(
                    f"{self.name} was turned off, but leaving Boost could not be sent"
                ) from boost_error

    async def async_set_gm(self, gm: float) -> None:
        """Set G/M in CCT mode, or remember it while the fixture is in HSI."""
        # Match the protocol's JavaScript-style half-up tie behavior while
        # keeping the cached Number state equal to what the fixture receives.
        target = max(-10, min(10, math.floor(gm + 0.5)))
        async with self._operation_lock:
            previous = self._preferred_gm
            state = self._state
            if state is None:
                raise AmaranConnectionError(f"{self.name} has not reported its state")
            if state.is_hsi:
                if target != previous:
                    self._preferred_gm = target
                    self._notify_listeners()
                return

            try:
                await self.async_set_cct(state.kelvin, state.intensity, target)
                self._preferred_gm = target
                if not await self._async_confirm_primary_state(
                    lambda: (
                        self._state is not None
                        and not self._state.is_hsi
                        and self._state.kelvin == state.kelvin
                        and self._state.intensity == state.intensity
                        and self._state.gm == target
                        and self._state.on == state.on
                    )
                ):
                    raise AmaranConnectionError(
                        f"{self.name} did not confirm its green/magenta setting"
                    )
            except BaseException:
                self._preferred_gm = previous
                raise

    async def async_refresh_state(
        self, attempts: int = 3, timeout: float = 0.7
    ) -> bool:
        """Refresh state without crossing another entity operation."""
        async with self._operation_lock:
            return await self._async_refresh_state(attempts, timeout)

    async def _async_refresh_state(
        self, attempts: int = 3, timeout: float = 0.7
    ) -> bool:
        """Ask the fixture for its state and wait for the report to arrive.

        Status requests are unacknowledged, so a single lost one used to leave
        Home Assistant showing stale state until the next poll a minute later.
        Waiting for the report also means a service call returns with the
        fixture's real state rather than the previous one.
        """
        return await self._async_confirm_primary_state(
            lambda: True, attempts=attempts, timeout=timeout
        )

    async def _async_confirm_primary_state(
        self,
        predicate: Callable[[], bool],
        *,
        attempts: int = 3,
        timeout: float = 0.7,
    ) -> bool:
        """Query until a fresh primary report matches an expected result."""
        for _ in range(attempts):
            self._state_received.clear()
            await self._async_send(telink.status_request(), retries=1)
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(timeout):
                    await self._state_received.wait()
                if predicate():
                    return True
        _LOGGER.debug("%s did not report its state", self.address)
        return False

    async def _async_wait_for(
        self, event: asyncio.Event, timeout: float = OPTIONAL_REPORT_TIMEOUT
    ) -> bool:
        """Wait for one typed report without confusing it with another page."""
        try:
            async with asyncio.timeout(timeout):
                await event.wait()
        except TimeoutError:
            return False
        return True

    async def _async_refresh_optional(
        self,
        payload: bytes,
        event: asyncio.Event,
        timeout: float = OPTIONAL_REPORT_TIMEOUT,
    ) -> bool:
        """Request an optional diagnostic page without affecting availability."""
        event.clear()
        await self._async_send(payload, retries=1)
        return await self._async_wait_for(event, timeout)

    async def _async_refresh_diagnostics(self, *, include_version: bool) -> None:
        """Refresh profile-gated Ace diagnostics independently of light state."""
        if (
            self.profile.supports_version
            and (include_version or self._version_state is None)
            and not await self._async_refresh_optional(
                telink.version_request(), self._version_received
            )
        ):
            _LOGGER.debug("%s did not report version data", self.address)
        if self.profile.supports_power and not await self._async_refresh_optional(
            telink.power_request(), self._power_received
        ):
            _LOGGER.debug("%s did not report power data", self.address)
        if self.profile.supports_fan and not await self._async_refresh_optional(
            telink.fan_request(), self._fan_received
        ):
            _LOGGER.debug("%s did not report fan data", self.address)

    # ─── Connection handling ─────────────────────────────────────────────────

    async def _async_connect(self) -> None:
        async with self._connect_lock:
            if self._closing:
                raise AmaranConnectionError(f"{self.name} is shutting down")
            if self._proxy is not None:
                return

            ble_device = bluetooth.async_ble_device_from_address(
                self.hass, self.address, connectable=True
            )
            if ble_device is None:
                if self._looks_unprovisioned():
                    raise AmaranNotProvisionedError(self.address)
                raise AmaranConnectionError(f"{self.address} is not in range")

            _LOGGER.debug("connecting to %s", self.address)
            try:
                client = await establish_connection(
                    BleakClient,
                    ble_device,
                    self.name,
                    self._on_disconnected,
                    ble_device_callback=lambda: bluetooth.async_ble_device_from_address(
                        self.hass, self.address, connectable=True
                    ),
                )
            except (BleakError, TimeoutError) as err:
                raise AmaranConnectionError(
                    f"could not connect to {self.address}: {err}"
                ) from err

            if self._closing:
                await self._close_client(client)
                raise AmaranConnectionError(f"{self.name} is shutting down")

            # Install the identity before starting notifications so a
            # disconnect -- or even malformed stored key material rejected by
            # ProxyClient construction -- cannot leak an unowned BLE client or
            # be mistaken for a stale callback from an older connection.
            self._client = client
            proxy: ProxyClient | None = None
            try:
                proxy = ProxyClient(
                    client,
                    net_key=self._net_key,
                    app_key=self._app_key,
                    device_keys={self._unicast_address: self._device_key},
                    local_address=self._local_address,
                    iv_index=self._iv_index,
                    sequence=self._sequence,
                    on_message=self._on_access_message,
                    on_sequence=self._on_sequence,
                    before_sequence=self._async_before_sequence,
                    replay_list=self._inbound_replay_list,
                    reassembler=self._segment_reassembler,
                )
                await proxy.start(subscribe_addresses=[self._unicast_address])
            except BaseException as err:
                if self._client is client:
                    self._client = None
                try:
                    if proxy is not None:
                        with contextlib.suppress(Exception):
                            await proxy.stop()
                finally:
                    await self._close_client(client)
                if isinstance(err, asyncio.CancelledError):
                    raise
                if isinstance(err, (BleakError, OSError, TimeoutError)):
                    if self._looks_unprovisioned():
                        raise AmaranNotProvisionedError(self.address) from err
                    raise AmaranConnectionError(
                        f"{self.address} did not expose the mesh proxy service: {err}"
                    ) from err
                if isinstance(
                    err,
                    (AmaranConnectionError, ProxyError, SequenceExhaustedError),
                ):
                    raise AmaranConnectionError(str(err)) from err
                raise

            if self._closing or self._client is not client or not client.is_connected:
                with contextlib.suppress(Exception):
                    await proxy.stop()
                if self._client is client:
                    self._client = None
                await self._close_client(client)
                if self._closing:
                    raise AmaranConnectionError(f"{self.name} is shutting down")
                raise AmaranConnectionError(
                    f"{self.address} disconnected while setting up its mesh proxy"
                )
            assert proxy is not None
            self._proxy = proxy
            self._reconnect_delay = RECONNECT_MIN_DELAY
            self._missed_polls = 0
            _LOGGER.debug("connected to %s", self.address)

        # Prime the cached state; without a report the entity stays unavailable.
        # Optional report pages are deliberately best-effort: a firmware that
        # omits battery/fan/version replies must not take down basic lighting.
        with contextlib.suppress(AmaranConnectionError):
            await self._async_refresh_state()
        with contextlib.suppress(AmaranConnectionError):
            await self._async_refresh_diagnostics(include_version=True)

    async def _async_disconnect(self) -> None:
        proxy, client = self._proxy, self._client
        self._proxy = None
        self._client = None
        self._clear_report_state()
        try:
            if proxy:
                with contextlib.suppress(Exception):
                    await proxy.stop()
        finally:
            if client:
                await self._close_client(client)

    async def _async_drop_failed_connection(self, proxy: ProxyClient) -> None:
        """Discard a failed link even when bleak omitted its disconnect callback."""
        if self._proxy is not proxy:
            return
        client = self._client
        self._proxy = None
        self._client = None
        self._clear_report_state()
        try:
            with contextlib.suppress(Exception):
                await proxy.stop()
        finally:
            try:
                if client:
                    await self._close_client(client)
            finally:
                self._schedule_reconnect()

    async def _close_client(self, client: BleakClient) -> None:
        # A disconnect can hang on a pending write; never let that stall unload.
        with contextlib.suppress(Exception, TimeoutError):
            async with asyncio.timeout(DISCONNECT_TIMEOUT):
                await client.disconnect()

    @callback
    def _on_disconnected(self, client: BleakClient) -> None:
        if self._closing or self._client is not client:
            return
        _LOGGER.debug("%s disconnected", self.address)
        proxy = self._proxy
        self._proxy = None
        self._client = None
        self._clear_report_state()
        if proxy is not None:
            self._schedule_proxy_cleanup(proxy)
        self._schedule_reconnect()

    @callback
    def _schedule_proxy_cleanup(self, proxy: ProxyClient) -> None:
        """Drain timers and callback tasks owned by a disconnected proxy."""
        task = self.hass.async_create_background_task(
            proxy.stop(), f"{DOMAIN} clean disconnected proxy {self.address}"
        )
        self._proxy_cleanup_tasks.add(task)

        def done(completed: asyncio.Task) -> None:
            self._proxy_cleanup_tasks.discard(completed)
            if completed.cancelled():
                return
            with contextlib.suppress(Exception):
                if err := completed.exception():
                    _LOGGER.debug(
                        "disconnected proxy cleanup for %s failed: %s",
                        self.address,
                        err,
                    )

        task.add_done_callback(done)

    @callback
    def _clear_report_state(self) -> None:
        """Invalidate every page from the disconnected mesh session."""
        changed = any(
            report is not None
            for report in (
                self._state,
                self._effect_state,
                self._boost_state,
                self._fan_state,
                self._power_state,
                self._version_state,
            )
        )
        self._state = None
        self._effect_state = None
        self._boost_state = None
        self._fan_state = None
        self._power_state = None
        self._version_state = None
        self._missed_polls = 0
        for event in (
            self._state_received,
            self._boost_received,
            self._fan_received,
            self._power_received,
            self._version_received,
        ):
            event.clear()
        if changed:
            self._notify_listeners()

    @callback
    def _schedule_reconnect(self) -> None:
        if self._closing or (self._reconnect_task and not self._reconnect_task.done()):
            return
        self._reconnect_task = self.hass.async_create_background_task(
            self._reconnect_loop(), f"{DOMAIN} reconnect {self.address}"
        )

    async def _reconnect_loop(self) -> None:
        while not self._closing and self._proxy is None:
            await asyncio.sleep(self._reconnect_delay)
            if self._closing:
                return
            try:
                async with self._operation_lock:
                    await self._async_connect()
            except AmaranNotProvisionedError:
                # Bluetooth service-data caches can be stale. Back off instead
                # of permanently terminalizing the entry, so a transient proxy
                # failure followed by a fresh 0x1828 advertisement self-heals.
                self._reconnect_delay = min(
                    self._reconnect_delay * 2, RECONNECT_MAX_DELAY
                )
                _LOGGER.warning(
                    "%s reports as unprovisioned; it must be re-added to Home "
                    "Assistant if it was factory reset; retrying in %ss in case "
                    "the Bluetooth advertisement was stale",
                    self.address,
                    self._reconnect_delay,
                )
            except AmaranConnectionError as err:
                self._reconnect_delay = min(
                    self._reconnect_delay * 2, RECONNECT_MAX_DELAY
                )
                _LOGGER.debug(
                    "reconnect to %s failed (%s); retrying in %ss",
                    self.address,
                    err,
                    self._reconnect_delay,
                )

    async def _poll_loop(self) -> None:
        """Refresh state periodically; this also keeps the link warm."""
        while not self._closing:
            await asyncio.sleep(
                POLL_INTERVAL
                if (self._state is not None or self._effect_state is not None)
                else INITIAL_POLL_INTERVAL
            )
            if self._closing:
                return
            if self._proxy is None:
                self._schedule_reconnect()
                continue
            try:
                async with self._operation_lock:
                    refreshed = await self._async_refresh_state(attempts=2)
                    if refreshed:
                        await self._async_refresh_diagnostics(include_version=False)
            except AmaranConnectionError as err:
                _LOGGER.debug("status poll for %s failed: %s", self.address, err)
                refreshed = False
            if refreshed:
                self._missed_polls = 0
                continue
            self._missed_polls += 1
            if self._missed_polls < MAX_MISSED_POLLS:
                continue
            proxy = self._proxy
            if proxy is not None:
                _LOGGER.debug(
                    "%s missed %d consecutive status polls; reconnecting",
                    self.address,
                    self._missed_polls,
                )
                await self._async_drop_failed_connection(proxy)

    def _looks_unprovisioned(self) -> bool:
        """True when the fixture advertises provisioning but not proxy service.

        A factory reset (or a re-pair with the amaran app) puts the fixture back
        into this state, and no key we hold will work on it again.
        """
        info = bluetooth.async_last_service_info(self.hass, self.address, False)
        if info is None:
            return False
        return (
            MESH_PROVISIONING_SERVICE in info.service_data
            and MESH_PROXY_SERVICE not in info.service_data
        )

    # ─── Messaging ───────────────────────────────────────────────────────────

    async def _async_send(self, payload: bytes, retries: int = 3) -> None:
        if self._closing:
            raise AmaranConnectionError(f"{self.name} is shutting down")
        if self._proxy is None:
            try:
                await self._async_connect()
            except AmaranNotProvisionedError as err:
                raise AmaranConnectionError(
                    f"{self.name} is no longer provisioned"
                ) from err
        proxy = self._proxy
        if proxy is None:
            raise AmaranConnectionError(f"{self.name} is not connected")
        try:
            await proxy.send_access(
                self._unicast_address, telink.OPCODE, payload, retries=retries
            )
        except (BleakError, OSError, TimeoutError) as err:
            await self._async_drop_failed_connection(proxy)
            raise AmaranConnectionError(
                f"failed to send to {self.name}: {err}"
            ) from err
        except (ProxyError, SequenceExhaustedError) as err:
            raise AmaranConnectionError(
                f"failed to send to {self.name}: {err}"
            ) from err

    @callback
    def _on_access_message(self, message: AccessMessage) -> None:
        if message.opcode != telink.OPCODE or message.src != self._unicast_address:
            return
        # Bit 79 is the proprietary write flag. ProxyClient already filters
        # our relayed network packets, and a fixture report clears this bit.
        # Refuse write-form echoes so a command cannot confirm itself.
        if len(message.parameters) != 10 or message.parameters[9] & 0x80:
            return
        report = telink.decode_report(
            message.parameters,
            protocol_version=(
                self._version_state.protocol_version
                if self._version_state is not None
                else 0
            ),
        )
        if report is None:
            return

        changed = False
        if isinstance(report, telink.LightState):
            previous_gm = self._preferred_gm
            previous_boost = self._boost_state
            if not report.is_hsi:
                self._preferred_gm = report.gm
            if self.profile.supports_boost and self._boost_state is None:
                boost_kelvin = (
                    report.kelvin
                    if not report.is_hsi
                    else self._default_effect_kelvin()
                )
                minimum = self.profile.boost_min_kelvin or self.profile.min_kelvin
                maximum = self.profile.boost_max_kelvin or self.profile.max_kelvin
                self._boost_state = telink.BoostState(
                    False, min(max(boost_kelvin, minimum), maximum), 100
                )
            changed = (
                report != self._state
                or self._effect_state is not None
                or self._boost_state != previous_boost
                or self._preferred_gm != previous_gm
            )
            self._state = report
            self._effect_state = None
            self._missed_polls = 0
            self._state_received.set()
        elif isinstance(report, telink.EffectState):
            if not self.profile.supports_effects:
                return
            previous_boost = self._boost_state
            new_effect = None if report.effect is telink.SystemEffect.OFF else report
            if self.profile.supports_boost and self._boost_state is None:
                minimum = self.profile.boost_min_kelvin or self.profile.min_kelvin
                maximum = self.profile.boost_max_kelvin or self.profile.max_kelvin
                self._boost_state = telink.BoostState(
                    False,
                    min(max(self._default_effect_kelvin(), minimum), maximum),
                    100,
                )
            changed = (
                new_effect != self._effect_state or self._boost_state != previous_boost
            )
            self._effect_state = new_effect
            self._missed_polls = 0
            self._state_received.set()
        elif isinstance(report, telink.BoostState):
            if not self.profile.supports_boost:
                return
            # The APK ignores this report's modal bit. Preserve the locally
            # commanded session state; an unsolicited report can update only
            # parameters and can never turn Boost on by itself.
            enabled = self._boost_state.enabled if self._boost_state else False
            report = replace(report, enabled=enabled)
            changed = report != self._boost_state
            self._boost_state = report
            self._boost_received.set()
        elif isinstance(report, telink.FanState):
            if not self.profile.supports_fan:
                return
            changed = report != self._fan_state
            self._fan_state = report
            self._fan_received.set()
        elif isinstance(report, telink.PowerState):
            if not self.profile.supports_power:
                return
            changed = report != self._power_state
            self._power_state = report
            self._power_received.set()
        elif isinstance(report, telink.VersionState):
            if not self.profile.supports_version:
                return
            changed = report != self._version_state
            self._version_state = report
            self._version_received.set()

        # Always release the matching waiter; an unchanged report still proves
        # the fixture answered the request.
        if changed:
            self._notify_listeners()

    @callback
    def _on_sequence(self, sequence: int) -> None:
        self._sequence = sequence
        assert self._sequence_reservation is not None
        self._sequence_reservation.mark_next(sequence)

    async def _async_before_sequence(self, sequence: int) -> None:
        """Reserve the sequence's block before the proxy transmits it."""
        assert self._sequence_reservation is not None
        try:
            await self._sequence_reservation.ensure_reserved(sequence)
        except OSError as err:
            raise AmaranConnectionError(
                "Bluetooth Mesh sequence reservation could not be persisted"
            ) from err

    async def _async_save_sequence(self, data: dict[str, int]) -> None:
        await _async_save_sequence_stores(
            self.hass,
            self._sequence_store_id,
            self._store,
            self._compatibility_sequence_store_id,
            self._compatibility_store,
            data,
        )

    @callback
    def _notify_listeners(self) -> None:
        for listener in list(self._listeners):
            listener()


async def async_release_node(
    hass: HomeAssistant,
    address: str,
    *,
    net_key: bytes,
    app_key: bytes,
    device_key: bytes,
    unicast_address: int,
    local_address: int,
    iv_index: int,
    sequence_store_id: str,
    compatibility_sequence_store_id: str | None = None,
    minimum_sequence: int = 0,
) -> bool:
    """Factory-reset the node so it stops belonging to our mesh.

    Without this, deleting the config entry would throw away the only copy of
    the keys and strand the fixture in a network nothing can talk to -- the
    owner would have to reset it by hand before Home Assistant or the amaran
    app could adopt it again.
    """
    ble_device = bluetooth.async_ble_device_from_address(
        hass, address, connectable=True
    )
    if ble_device is None:
        return False

    client = await establish_connection(BleakClient, ble_device, address)
    try:
        store = _sequence_store(hass, sequence_store_id)
        compatibility_sequence_store_id = (
            compatibility_sequence_store_id or sequence_store_id
        )
        compatibility_store = (
            None
            if compatibility_sequence_store_id == sequence_store_id
            else _sequence_store(hass, compatibility_sequence_store_id)
        )

        async def save(data: dict[str, int]) -> None:
            await _async_save_sequence_stores(
                hass,
                sequence_store_id,
                store,
                compatibility_sequence_store_id,
                compatibility_store,
                data,
            )

        reservation = SequenceReservation.create(
            await _async_load_sequence_stores(store, compatibility_store),
            save,
            block_size=SEQUENCE_CHECKPOINT,
            minimum_sequence=minimum_sequence,
        )
        proxy = ProxyClient(
            client,
            net_key=net_key,
            app_key=app_key,
            device_keys={unicast_address: device_key},
            local_address=local_address,
            iv_index=iv_index,
            sequence=reservation.next_sequence,
            on_sequence=reservation.mark_next,
            before_sequence=reservation.ensure_reserved,
        )
        await proxy.start(subscribe_addresses=[unicast_address])
        try:
            reset_acknowledged = await ConfigClient(proxy, unicast_address).node_reset()
            # Many nodes reset before their status reaches the proxy. In that
            # case a link drop is the positive signal that the command took.
            for _ in range(30):
                if not client.is_connected:
                    break
                await asyncio.sleep(0.1)
            reset_confirmed = reset_acknowledged or not client.is_connected
        finally:
            with contextlib.suppress(Exception):
                await proxy.stop()
    finally:
        with contextlib.suppress(Exception, TimeoutError):
            async with asyncio.timeout(DISCONNECT_TIMEOUT):
                await client.disconnect()
    return reset_confirmed


async def async_configure_node(
    client: BleakClient,
    *,
    net_key: bytes,
    app_key: bytes,
    device_key: bytes,
    unicast_address: int,
    local_address: int,
    iv_index: int,
    sequence: int = 0,
    on_sequence: Callable[[int], None] | None = None,
    before_sequence: Callable[[int], Awaitable[None]] | None = None,
) -> int:
    """Give a freshly provisioned node its AppKey and model bindings.

    Until this runs the node holds a NetKey but no application key, so it
    answers configuration messages and ignores every light command.
    """
    proxy = ProxyClient(
        client,
        net_key=net_key,
        app_key=app_key,
        device_keys={unicast_address: device_key},
        local_address=local_address,
        iv_index=iv_index,
        sequence=sequence,
        on_sequence=on_sequence,
        before_sequence=before_sequence,
    )
    started = False
    try:
        await proxy.start(subscribe_addresses=[unicast_address])
        started = True
        config = ConfigClient(proxy, unicast_address)
        composition = await config.get_composition_data()
        _LOGGER.debug(
            "node %#06x: company=%#06x %d element(s)",
            unicast_address,
            composition.company_id,
            len(composition.elements),
        )
        await config.add_app_key(app_key)
        await config.bind_all_models(composition)
        # Binding failures for unsupported models are expected and skipped.
        # A real status reply proves that the AppKey reached whichever model
        # routes amaran's proprietary opcode to the physical LEDs.
        await proxy.request(
            unicast_address,
            telink.OPCODE,
            telink.status_request(),
            expect_opcode=telink.OPCODE,
            response_matcher=lambda message: (
                telink.decode_status(message.parameters) is not None
            ),
            timeout=6.0,
        )
    except Exception as err:
        raise NodeConfigurationError(str(err), proxy.sequence) from err
    finally:
        if started:
            with contextlib.suppress(Exception):
                await proxy.stop()
    return proxy.sequence


async def async_configure_stored_node(
    hass: HomeAssistant,
    address: str,
    name: str,
    *,
    net_key: bytes,
    app_key: bytes,
    device_key: bytes,
    unicast_address: int,
    local_address: int,
    iv_index: int,
    sequence: int,
    sequence_store_id: str,
    compatibility_sequence_store_id: str | None = None,
) -> int:
    """Resume post-provision configuration retained in a config entry."""
    ble_device = bluetooth.async_ble_device_from_address(
        hass, address, connectable=True
    )
    if ble_device is None:
        raise AmaranConnectionError(f"{address} is not in range")

    try:
        client = await establish_connection(BleakClient, ble_device, name)
    except (BleakError, TimeoutError) as err:
        raise AmaranConnectionError(f"could not connect to {address}: {err}") from err

    try:
        store = _sequence_store(hass, sequence_store_id)
        compatibility_sequence_store_id = (
            compatibility_sequence_store_id or sequence_store_id
        )
        compatibility_store = (
            None
            if compatibility_sequence_store_id == sequence_store_id
            else _sequence_store(hass, compatibility_sequence_store_id)
        )

        async def save(data: dict[str, int]) -> None:
            await _async_save_sequence_stores(
                hass,
                sequence_store_id,
                store,
                compatibility_sequence_store_id,
                compatibility_store,
                data,
            )

        reservation = SequenceReservation.create(
            await _async_load_sequence_stores(store, compatibility_store),
            save,
            block_size=SEQUENCE_CHECKPOINT,
            minimum_sequence=sequence,
        )
        return await async_configure_node(
            client,
            net_key=net_key,
            app_key=app_key,
            device_key=device_key,
            unicast_address=unicast_address,
            local_address=local_address,
            iv_index=iv_index,
            sequence=reservation.next_sequence,
            on_sequence=reservation.mark_next,
            before_sequence=reservation.ensure_reserved,
        )
    finally:
        with contextlib.suppress(Exception, TimeoutError):
            async with asyncio.timeout(DISCONNECT_TIMEOUT):
                await client.disconnect()
