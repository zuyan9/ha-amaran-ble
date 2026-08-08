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
from dataclasses import dataclass, replace

from bleak import BleakClient
from bleak.exc import BleakError
from bleak_retry_connector import establish_connection
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .amaranble import highspeed, network, pixelfx, systemfx2, telink
from .amaranble.config_client import ConfigClient
from .amaranble.gatt import (
    MESH_PROVISIONING_SERVICE,
    MESH_PROXY_SERVICE,
    PROVISIONING_DATA_IN,
    PROVISIONING_DATA_OUT,
    PROXY_DATA_IN,
    PROXY_DATA_OUT,
)
from .amaranble.proxy import AccessMessage, ProxyClient, ProxyError
from .amaranble.sequence import SequenceExhaustedError, SequenceReservation
from .brightness import intensities_have_same_brightness
from .const import (
    DOMAIN,
    INITIAL_POLL_INTERVAL,
    POLL_INTERVAL,
    RECONNECT_MAX_DELAY,
    RECONNECT_MIN_DELAY,
    SEQUENCE_CHECKPOINT,
)
from .profiles import FixtureProfile
from .resolver import MeshProxyCandidate, async_mesh_proxy_candidates

_LOGGER = logging.getLogger(__name__)

DISCONNECT_TIMEOUT = 5.0
MAX_MISSED_POLLS = 3
OPTIONAL_REPORT_TIMEOUT = 0.8
FAN_APPLY_SETTLE = 0.2

EFFECT_COLOR_MODE_CCT = "cct"
EFFECT_COLOR_MODE_HSI = "hsi"
LEGACY_DUAL_COLOR_EFFECTS = frozenset(
    {
        telink.SystemEffect.FAULTY_BULB,
        telink.SystemEffect.PULSING,
        telink.SystemEffect.STROBE,
        telink.SystemEffect.EXPLOSION,
        telink.SystemEffect.WELDING,
    }
)


def _effect_state_matches(
    report: telink.EffectState | None,
    expected: telink.EffectState,
) -> bool:
    """Compare a command-7 state with HA-equivalent global intensity."""
    return (
        report is not None
        and intensities_have_same_brightness(report.intensity, expected.intensity)
        and replace(report, intensity=expected.intensity) == expected
    )


def _effect2_state_matches(
    report: systemfx2.SystemEffect2State | None,
    expected: systemfx2.SystemEffect2State,
) -> bool:
    """Compare a command-34 state with HA-equivalent global intensity."""
    return (
        report is not None
        and intensities_have_same_brightness(report.intensity, expected.intensity)
        and replace(report, intensity=expected.intensity) == expected
    )


@dataclass(frozen=True, slots=True)
class PixelRuntimeState:
    """Merged command-33 pages for one active pixel effect.

    Pixel effects are reported as one control page plus one or more colour
    pages.  Home Assistant needs a single primary light state, so the runtime
    retains the latest page of each kind while still exposing the effect's
    playback and global intensity as one object.
    """

    effect: pixelfx.PixelEffect
    playback: pixelfx.PixelPlayback
    intensity: int | None
    pages: tuple[pixelfx.PixelEffectState, ...]

    @property
    def on(self) -> bool | None:
        """Return the proven pixel power state, or unknown for CONTINUE pages."""
        if self.playback is pixelfx.PixelPlayback.CONTINUE:
            # The app treats CONTINUE as non-sleep, but it is emitted by colour
            # pages specifically to preserve the existing playback state. A
            # lone continuation report therefore cannot prove on versus off.
            return None
        return self.playback is not pixelfx.PixelPlayback.STOP


def _pixel_page_key(state: pixelfx.PixelEffectState) -> tuple[int, int]:
    """Return a stable key/order for one command-33 report page."""
    if state.effect is pixelfx.PixelEffect.RAINBOW:
        return (2, 0)
    if state.packet_type is pixelfx.PixelPacketType.COLOR:
        return (0, state.serial or 0)
    if state.packet_type is pixelfx.PixelPacketType.BASE:
        return (1, 0)
    return (2, 0)


def _merge_pixel_page(
    current: PixelRuntimeState | None,
    report: pixelfx.PixelEffectState,
) -> PixelRuntimeState:
    """Merge a command-33 page without discarding other reported settings."""
    pages = (
        {_pixel_page_key(page): page for page in current.pages}
        if current is not None and current.effect is report.effect
        else {}
    )
    pages[_pixel_page_key(report)] = report
    ordered = tuple(page for _, page in sorted(pages.items()))
    control = next(
        (
            page
            for page in ordered
            if page.effect is pixelfx.PixelEffect.RAINBOW
            or page.packet_type is pixelfx.PixelPacketType.CONTROL
        ),
        None,
    )
    playback = (
        control.playback
        if control is not None
        else (
            current.playback
            if current is not None and current.effect is report.effect
            else report.playback
        )
    )
    intensity = next(
        (
            page.brightness
            for page in ordered
            if page.brightness is not None
            and (
                page.effect is not pixelfx.PixelEffect.PIXEL_FIRE
                or page.packet_type is pixelfx.PixelPacketType.BASE
            )
        ),
        None,
    )
    return PixelRuntimeState(report.effect, playback, intensity, ordered)


def _decode_pixel_sequence(payloads: tuple[bytes, ...]) -> PixelRuntimeState:
    """Decode the app's complete multi-page pixel-effect sequence."""
    state: PixelRuntimeState | None = None
    for payload in payloads:
        report = pixelfx.decode(payload)
        assert report is not None
        state = _merge_pixel_page(state, report)
    assert state is not None
    return state


def _pixel_pages_complete(state: PixelRuntimeState) -> bool:
    """Return whether every page required by the reported program is cached."""
    keys = {_pixel_page_key(page) for page in state.pages}
    if state.effect is pixelfx.PixelEffect.RAINBOW:
        return keys == {(2, 0)}
    if (2, 0) not in keys:
        return False
    if state.effect in {
        pixelfx.PixelEffect.COLOR_FADE,
        pixelfx.PixelEffect.COLOR_CYCLE,
    }:
        control = next(
            page
            for page in state.pages
            if page.packet_type is pixelfx.PixelPacketType.CONTROL
        )
        return (
            control.color_count is not None
            and control.color_count >= 2
            and all((0, serial) in keys for serial in range(control.color_count))
        )
    if state.effect is pixelfx.PixelEffect.PIXEL_FIRE:
        return {(0, 0), (1, 0), (2, 0)} <= keys
    control = next(
        page
        for page in state.pages
        if page.packet_type is pixelfx.PixelPacketType.CONTROL
    )
    if control.group not in {0, 1}:
        return False
    single_group_pages = {
        pixelfx.PixelEffect.ONE_PIXEL_CHASE: 2,
        pixelfx.PixelEffect.TWO_PIXEL_CHASE: 3,
        pixelfx.PixelEffect.THREE_PIXEL_CHASE: 4,
    }[state.effect]
    color_pages = (
        single_group_pages if control.group == 0 else (single_group_pages * 2) - 1
    )
    return all((0, serial) in keys for serial in range(color_pages))


def _pixel_payloads(
    current: PixelRuntimeState,
    *,
    intensity: int | None,
    on: bool,
) -> tuple[bytes, ...]:
    """Rebuild a complete pixel program while preserving its page settings."""
    # A reconnect may yield only the control report. Fall back to the app's
    # proven defaults rather than inventing missing colour pages.
    defaults = _decode_pixel_sequence(pixelfx.effect(current.effect, on=on))
    source = current if _pixel_pages_complete(current) else defaults
    playback = pixelfx.PixelPlayback.RUNNING if on else pixelfx.PixelPlayback.STOP
    payloads: list[bytes] = []
    source_pages = source.pages
    if source.effect in {
        pixelfx.PixelEffect.COLOR_FADE,
        pixelfx.PixelEffect.COLOR_CYCLE,
    }:
        control = next(
            page
            for page in source.pages
            if page.packet_type is pixelfx.PixelPacketType.CONTROL
        )
        assert control.color_count is not None
        source_pages = tuple(
            page
            for page in source.pages
            if page.packet_type is not pixelfx.PixelPacketType.COLOR
            or (page.serial is not None and page.serial < control.color_count)
        )
    elif source.effect in {
        pixelfx.PixelEffect.ONE_PIXEL_CHASE,
        pixelfx.PixelEffect.TWO_PIXEL_CHASE,
        pixelfx.PixelEffect.THREE_PIXEL_CHASE,
    }:
        control = next(
            page
            for page in source.pages
            if page.packet_type is pixelfx.PixelPacketType.CONTROL
        )
        assert control.group in {0, 1}
        single_group_pages = {
            pixelfx.PixelEffect.ONE_PIXEL_CHASE: 2,
            pixelfx.PixelEffect.TWO_PIXEL_CHASE: 3,
            pixelfx.PixelEffect.THREE_PIXEL_CHASE: 4,
        }[source.effect]
        color_count = (
            single_group_pages if control.group == 0 else (single_group_pages * 2) - 1
        )
        source_pages = tuple(
            page
            for page in source.pages
            if page.packet_type is not pixelfx.PixelPacketType.COLOR
            or (page.serial is not None and page.serial < color_count)
        )
    for page in source_pages:
        final_page = (
            page.effect is pixelfx.PixelEffect.RAINBOW
            or page.packet_type is pixelfx.PixelPacketType.CONTROL
        )
        updated = replace(
            page,
            playback=(playback if final_page else pixelfx.PixelPlayback.CONTINUE),
            brightness=(page.brightness if intensity is None else intensity)
            if page.brightness is not None
            else None,
        )
        payloads.append(pixelfx.encode(updated))
    return tuple(payloads)


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


async def _async_establish_candidate(
    hass: HomeAssistant,
    candidate: MeshProxyCandidate,
    name: str,
    disconnected_callback: Callable[[BleakClient], None] | None = None,
) -> BleakClient:
    """Connect through HA's best current scanner source for one address."""
    return await establish_connection(
        BleakClient,
        candidate.ble_device,
        name,
        disconnected_callback,
        ble_device_callback=lambda: bluetooth.async_ble_device_from_address(
            hass, candidate.address, connectable=True
        ),
        # Provisioning and Proxy are mutually exclusive GATT layouts. A
        # factory reset changes the services, so an otherwise valid BlueZ
        # cache can conceal the authoritative provisioning-only bearer.
        use_services_cache=False,
    )


def _client_exposes_only_provisioning_bearer(client: BleakClient) -> bool:
    """Return whether fresh GATT discovery proves the node was factory-reset."""
    try:
        services = client.services
        has_complete_provisioning_bearer = (
            services.get_service(MESH_PROVISIONING_SERVICE) is not None
            and services.get_characteristic(PROVISIONING_DATA_IN) is not None
            and services.get_characteristic(PROVISIONING_DATA_OUT) is not None
        )
        has_any_proxy_bearer = (
            services.get_service(MESH_PROXY_SERVICE) is not None
            or services.get_characteristic(PROXY_DATA_IN) is not None
            or services.get_characteristic(PROXY_DATA_OUT) is not None
        )
    except AttributeError, BleakError, RuntimeError:
        # An unavailable/incomplete service cache is not proof of reset. The
        # normal proxy startup below will supply the connection error instead.
        return False
    return has_complete_provisioning_bearer and not has_any_proxy_bearer


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
        transport_address: str | None = None,
        on_not_provisioned: Callable[[], None] | None = None,
        on_provisioned: Callable[[], None] | None = None,
    ) -> None:
        self.hass = hass
        # This is the stable config-entry, entity, and device-registry identity.
        # A fixture may advertise through a changing random BLE address, which
        # is tracked separately and is never allowed to change those IDs.
        self.address = address
        self._transport_address = transport_address or address
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
        self._on_not_provisioned = on_not_provisioned
        self._on_provisioned = on_provisioned
        self._not_provisioned_reported = False
        # A GATT Proxy bearer alone does not prove that it belongs to this
        # private mesh: Telink's proxy-filter handshake deliberately tolerates
        # missing status replies. Only an accepted access-layer primary report
        # may mark the node recovered, once per membership episode.
        self._provisioned_reported = False

        # Keep the last steady state while a built-in effect is active. That
        # lets selecting HA's ``off`` effect return to the user's previous CCT
        # look instead of inventing a new one.
        self._state: telink.LightState | None = None
        self._effect_state: telink.EffectState | None = None
        self._effect2_state: systemfx2.SystemEffect2State | None = None
        self._pixel_state: PixelRuntimeState | None = None
        self._pixel_report_generation = 0
        self._pixel_page_generations: dict[tuple[int, int], int] = {}
        self._boost_state: telink.BoostState | None = None
        self._fan_state: telink.FanState | None = None
        self._power_state: telink.PowerState | None = None
        self._version_state: telink.VersionState | None = None
        self._version2_state: telink.Version2State | None = None
        self._high_speed_state: highspeed.HighSpeedMessage | None = None
        # HSI reports do not carry G/M, so retain the last CCT tint instead of
        # treating every switch to colour mode as a reset to neutral.
        self._preferred_gm = 0
        self._state_received = asyncio.Event()
        self._boost_received = asyncio.Event()
        self._fan_received = asyncio.Event()
        self._power_received = asyncio.Event()
        self._version_received = asyncio.Event()
        self._version2_received = asyncio.Event()
        self._high_speed_received = asyncio.Event()
        self._listeners: list[Callable[[], None]] = []

    # ─── Public surface ──────────────────────────────────────────────────────

    @property
    def state(self) -> telink.LightState | None:
        return self._state

    @property
    def effect_state(
        self,
    ) -> telink.EffectState | systemfx2.SystemEffect2State | PixelRuntimeState | None:
        """Return the active built-in effect regardless of protocol generation."""
        return self._effect_state or self._effect2_state or self._pixel_state

    @property
    def effect2_state(self) -> systemfx2.SystemEffect2State | None:
        """Return the active command-34 effect state, if any."""
        return self._effect2_state

    @property
    def pixel_state(self) -> PixelRuntimeState | None:
        """Return the merged command-33 pixel-effect state, if active."""
        return self._pixel_state

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
    def version2_state(self) -> telink.Version2State | None:
        """Return runtime-discovered advanced effect and pixel capabilities."""
        return self._version2_state

    @property
    def high_speed_state(self) -> highspeed.HighSpeedMessage | None:
        """Return the last commanded or reported high-speed-photo state."""
        return self._high_speed_state

    @property
    def preferred_gm(self) -> float:
        """Return the last known or requested green/magenta adjustment."""
        return self._preferred_gm

    @property
    def green_magenta_min(self) -> int:
        """Return the cataloged minimum normalized G/M value."""
        color = self.profile.catalog_capabilities.steady_color
        return color.gm_min - 10 if color.gm else -10

    @property
    def green_magenta_max(self) -> int:
        """Return the cataloged maximum normalized G/M value."""
        color = self.profile.catalog_capabilities.steady_color
        return color.gm_max - 10 if color.gm else 10

    @property
    def available(self) -> bool:
        return self._proxy is not None and (
            self._state is not None
            or self._effect_state is not None
            or self._effect2_state is not None
            or self._pixel_state is not None
        )

    @property
    def connected(self) -> bool:
        """Return whether the current BLE Mesh proxy link is usable."""
        return self._proxy is not None

    @property
    def using_alternate_address(self) -> bool:
        """Return whether the last selected transport differs from stable identity."""
        return self._transport_address.casefold() != self.address.casefold()

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
        return (self._effect_state is not None and self._effect_state.on) or (
            self._effect2_state is not None
            and self._effect2_state.on
            and self._effect2_state.frequency is not None
        )

    @property
    def effect_color_temperature_available(self) -> bool:
        """Whether the active effect carries an adjustable CCT field."""
        return (
            self._effect_state is not None
            and self._effect_state.on
            and self._effect_state.kelvin is not None
        ) or (
            self._effect2_state is not None
            and self._effect2_state.on
            and self._effect2_state.kelvin is not None
        )

    @property
    def effect_hue_available(self) -> bool:
        """Whether the active effect carries an adjustable HSI hue."""
        return (
            self._effect_state is not None
            and self._effect_state.on
            and self._effect_state.hue is not None
        ) or (
            self._effect2_state is not None
            and self._effect2_state.on
            and self._effect2_state.hue is not None
        )

    @property
    def effect_saturation_available(self) -> bool:
        """Whether the active effect carries adjustable saturation."""
        return (
            self._effect_state is not None
            and self._effect_state.on
            and self._effect_state.saturation is not None
        ) or (
            self._effect2_state is not None
            and self._effect2_state.on
            and self._effect2_state.saturation is not None
        )

    @property
    def effect_gm_available(self) -> bool:
        """Whether the active effect carries an adjustable G/M field."""
        return self.profile.supports_gm and (
            (
                self._effect_state is not None
                and self._effect_state.on
                and self._effect_state.gm is not None
            )
            or (
                self._effect2_state is not None
                and self._effect2_state.on
                and self._effect2_state.gm is not None
            )
        )

    @property
    def effect_variant_options(self) -> tuple[str, ...]:
        """Return the active effect's app-defined colour presets."""
        state = self._effect_state
        if state is None or not state.on:
            return ()
        if state.effect in {
            telink.SystemEffect.TV,
            telink.SystemEffect.CANDLE,
            telink.SystemEffect.FIRE,
        }:
            return ("warmer", "natural", "cooler")
        if state.effect is telink.SystemEffect.FIREWORKS:
            return ("warmer", "cooler", "multi")
        if state.effect is telink.SystemEffect.CLUB_LIGHTS:
            return ("3", "6", "9", "12", "15", "18", "24", "36")
        if state.effect is telink.SystemEffect.COP_CAR:
            return (
                "red",
                "blue",
                "red_blue",
                "blue_white",
                "red_blue_white",
            )
        return ()

    @property
    def effect_color_mode_options(self) -> tuple[str, ...]:
        """Return color representations supported by the active legacy effect."""
        state = self._effect_state
        if (
            state is None
            or not state.on
            or state.effect not in LEGACY_DUAL_COLOR_EFFECTS
        ):
            return ()
        options: list[str] = []
        if self.profile.supports_cct:
            options.append(EFFECT_COLOR_MODE_CCT)
        if self.profile.supports_color:
            options.append(EFFECT_COLOR_MODE_HSI)
        return tuple(options)

    @property
    def effect_color_mode(self) -> str | None:
        """Return the active legacy effect's CCT or HSI representation."""
        state = self._effect_state
        if state is None or not state.on:
            return None
        mode = EFFECT_COLOR_MODE_HSI if state.mode == 1 else EFFECT_COLOR_MODE_CCT
        return mode if mode in self.effect_color_mode_options else None

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
        await self._async_send(
            telink.cct(
                kelvin,
                intensity,
                gm,
                gm_flag=bool(
                    self.profile.catalog_capabilities.steady_color.gm_v2_version
                ),
            )
        )

    async def async_set_hsi(
        self, hue: float, saturation: float, intensity: int
    ) -> None:
        await self._async_send(telink.hsi(hue, saturation, intensity))

    async def async_set_high_speed(self, enabled: bool) -> None:
        """Set the cataloged high-speed-photography mode.

        The app artifact exposes command 53 as a one-bit state write but does
        not contain a separate read request. Keep the successfully transmitted
        state locally, as the app does for its similarly modal Boost command;
        a later fixture report can still replace it.
        """
        capability = self.profile.catalog_capabilities.high_speed_photography
        if not capability.supported:
            raise AmaranConnectionError(
                f"high-speed photography is not enabled for {self.name}"
            )
        payload = highspeed.build_high_speed(enabled)
        async with self._operation_lock:
            await self._async_send(payload)
            state = highspeed.decode_high_speed(payload)
            assert state is not None
            changed = state != self._high_speed_state
            self._high_speed_state = state
            if changed:
                self._notify_listeners()

    def _effect_payload(
        self,
        state: telink.EffectState,
        *,
        intensity: float | None = None,
        frequency: float | None = None,
        kelvin: float | None = None,
        variant: float | None = None,
        mode: float | None = None,
        hue: float | None = None,
        saturation: float | None = None,
        gm: float | None = None,
        gm_flag: int | bool | None = None,
    ) -> bytes:
        """Rebuild the active effect's complete state packet."""
        return telink.effect(
            state.effect,
            intensity=state.intensity if intensity is None else intensity,
            frequency=state.frequency if frequency is None else frequency,
            speed=(
                state.speed
                if state.effect is telink.SystemEffect.WELDING
                else state.speed or None
            ),
            trigger=state.trigger,
            kelvin=(
                state.kelvin or self._default_effect_kelvin()
                if kelvin is None
                else kelvin
            ),
            variant=state.variant if variant is None else variant,
            mode=state.mode if mode is None else mode,
            hue=(state.hue or 0) if hue is None else hue,
            saturation=state.saturation if saturation is None else saturation,
            gm=(state.gm if state.gm is not None else 100) if gm is None else gm,
            gm_flag=state.gm_flag if gm_flag is None else gm_flag,
        )

    def _effect2_payloads(
        self,
        state: systemfx2.SystemEffect2State,
        *,
        on: bool | None = None,
        intensity: int | None = None,
        frequency: int | None = None,
        kelvin: int | None = None,
        gm: int | None = None,
        hue: int | None = None,
        saturation: int | None = None,
    ) -> tuple[bytes, ...]:
        """Rebuild a command-34 effect without discarding reported fields."""
        return systemfx2.effect2(
            state.effect,
            on=state.on if on is None else on,
            intensity=state.intensity if intensity is None else intensity,
            frequency=state.frequency if frequency is None else frequency,
            speed=state.speed,
            mode=state.mode,
            kelvin=state.kelvin if kelvin is None else kelvin,
            gm=state.gm if gm is None else gm,
            hue=state.hue if hue is None else hue,
            saturation=(state.saturation if saturation is None else saturation),
            center_kelvin=state.center_kelvin,
            min_kelvin=state.min_kelvin,
            max_kelvin=state.max_kelvin,
            min_hue=state.min_hue,
            max_hue=state.max_hue,
            min_intensity=state.min_intensity,
            gap_time=state.gap_time,
            min_gap_time=state.min_gap_time,
            decay=state.decay,
            color=state.color,
            gel_kelvin=state.gel_kelvin,
            gel_origin=state.gel_origin,
            gel_type=state.gel_type,
            variant=state.variant,
        )

    @staticmethod
    def _decode_effect2_sequence(
        payloads: tuple[bytes, ...],
    ) -> systemfx2.SystemEffect2State:
        """Decode and merge a complete command-34 packet sequence."""
        state: systemfx2.SystemEffect2State | None = None
        for payload in payloads:
            decoded = systemfx2.decode_effect2(payload)
            assert decoded is not None
            state = systemfx2.merge_effect2_states(state, decoded)
        assert state is not None
        return state

    async def _async_update_effect2_unlocked(
        self,
        *,
        error_label: str,
        frequency: int | None = None,
        kelvin: int | None = None,
        gm: int | None = None,
        hue: int | None = None,
        saturation: int | None = None,
    ) -> None:
        """Rewrite and confirm a complete command-34 effect state."""
        state = self._effect2_state
        if state is None or not state.on:
            raise AmaranConnectionError(
                f"{self.name} is not running an active generation-II effect"
            )
        payloads = self._effect2_payloads(
            state,
            frequency=frequency,
            kelvin=kelvin,
            gm=gm,
            hue=hue,
            saturation=saturation,
        )
        expected = self._decode_effect2_sequence(payloads)
        for payload in payloads:
            await self._async_send(payload)
        if not await self._async_confirm_primary_state(
            lambda: _effect2_state_matches(self._effect2_state, expected)
        ):
            raise AmaranConnectionError(
                f"{self.name} did not confirm its effect {error_label}"
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
        self,
        effect: telink.SystemEffect
        | systemfx2.SystemEffect2
        | pixelfx.PixelEffect
        | str,
        *,
        intensity: int | None = None,
    ) -> None:
        """Select a built-in effect as part of a Home Assistant turn-on."""
        if not self.profile.supports_effects:
            raise AmaranConnectionError(f"effects are not enabled for {self.name}")
        try:
            selected = telink.SystemEffect(effect)
        except ValueError:
            try:
                selected2 = systemfx2.SystemEffect2(effect)
            except ValueError:
                try:
                    selected_pixel = pixelfx.PixelEffect(effect)
                except ValueError as err:
                    raise AmaranConnectionError(
                        f"{effect!r} is not a supported effect for {self.name}"
                    ) from err
                if selected_pixel.value not in self.profile.pixel_effects:
                    raise AmaranConnectionError(
                        f"{selected_pixel.value!r} is not a supported effect for "
                        f"{self.name}"
                    ) from None
                async with self._operation_lock:
                    await self._async_apply_pixel_effect_unlocked(
                        selected_pixel, intensity=intensity
                    )
                return
            else:
                if selected2.value not in self.profile.system_effects2:
                    raise AmaranConnectionError(
                        f"{selected2.value!r} is not a supported effect for {self.name}"
                    ) from None
                async with self._operation_lock:
                    await self._async_apply_effect2_unlocked(
                        selected2, intensity=intensity
                    )
                return
        if selected.value not in self.profile.effects and not (
            selected is telink.SystemEffect.OFF
            and selected.value in self.profile.all_effects
        ):
            raise AmaranConnectionError(
                f"{selected.value!r} is not a supported effect for {self.name}"
            )
        async with self._operation_lock:
            if selected is telink.SystemEffect.OFF:
                await self._async_exit_effect(intensity=intensity)
                return

            current = self._effect_state
            active = self.effect_state
            if self._boost_state is not None and self._boost_state.enabled:
                await self._async_set_boost_unlocked(False)
            output_was_on = (
                active.on
                if active is not None
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
                        active.intensity
                        if active is not None
                        and active.intensity is not None
                        and active.intensity > 0
                        else (
                            self._state.intensity
                            if self._state is not None and self._state.intensity > 0
                            else 180
                        )
                    )
                )
                dual_color_effect = selected in LEGACY_DUAL_COLOR_EFFECTS
                use_hsi = (
                    dual_color_effect
                    and self.profile.supports_color
                    and (self._state is None or self._state.is_hsi)
                )
                gm_v2 = bool(
                    self.profile.catalog_capabilities.steady_color.gm_v2_version
                )
                effect_gm = (
                    math.floor((self._preferred_gm + 10.0) * 10 + 0.5)
                    if gm_v2
                    else 10 * math.floor((self._preferred_gm + 10.0) + 0.5)
                )
                payload = telink.effect(
                    selected,
                    intensity=expected_intensity,
                    frequency=5,
                    kelvin=self._default_effect_kelvin(),
                    mode=1 if use_hsi else 0,
                    hue=(
                        self._state.hue
                        if self._state is not None and self._state.is_hsi
                        else 0
                    ),
                    saturation=(
                        self._state.saturation
                        if self._state is not None and self._state.is_hsi
                        else 100
                    ),
                    gm=effect_gm,
                    gm_flag=gm_v2,
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
                    return _effect_state_matches(report, expected_effect)
                # Firmware chooses and normalizes opaque defaults (notably
                # speed/trigger) when a different effect is selected. Confirm
                # the user-visible transition without pretending those defaults
                # are fixed across firmware versions.
                return (
                    report is not None
                    and report.on
                    and report.effect is selected
                    and intensities_have_same_brightness(
                        report.intensity, expected_intensity
                    )
                )

            if not await self._async_confirm_primary_state(effect_matches):
                raise AmaranConnectionError(
                    f"{self.name} did not confirm effect {selected.value}"
                )

    async def _async_apply_pixel_effect_unlocked(
        self,
        selected: pixelfx.PixelEffect,
        *,
        intensity: int | None,
    ) -> None:
        """Select a defaults-proven command-33 pixel program."""
        current = self._pixel_state
        active = self.effect_state
        if self._boost_state is not None and self._boost_state.enabled:
            await self._async_set_boost_unlocked(False)
        output_was_on = (
            active.on
            if active is not None
            else self._state is not None and self._state.on
        )
        expected_intensity = (
            intensity
            if intensity is not None
            else (
                active.intensity
                if active is not None
                and active.intensity is not None
                and active.intensity > 0
                else (
                    self._state.intensity
                    if self._state is not None and self._state.intensity > 0
                    else 180
                )
            )
        )
        if current is not None and current.effect is selected:
            payloads = _pixel_payloads(
                current,
                intensity=expected_intensity,
                on=True,
            )
        else:
            defaults = _decode_pixel_sequence(pixelfx.effect(selected))
            payloads = _pixel_payloads(
                defaults,
                intensity=expected_intensity,
                on=True,
            )
        expected = _decode_pixel_sequence(payloads)
        report_generation = self._pixel_report_generation
        for payload in payloads:
            await self._async_send(payload)
        if not output_was_on:
            await self.async_turn_on()

        def pixel_effect_matches() -> bool:
            report = self._pixel_state
            if (
                report is None
                or not report.on
                or report.effect is not selected
                or self._pixel_page_generations.get((2, 0), 0) <= report_generation
            ):
                return False
            if selected is pixelfx.PixelEffect.RAINBOW:
                return intensities_have_same_brightness(
                    report.intensity, expected_intensity
                )
            # Most status replies include only the control page, which carries
            # no brightness. Preserve that proven fallback, but when the
            # fixture also returns a fresh brightness-bearing colour/base page,
            # it must project to the brightness requested by Home Assistant.
            return all(
                intensities_have_same_brightness(page.brightness, expected_intensity)
                for page in report.pages
                if page.brightness is not None
                and self._pixel_page_generations.get(_pixel_page_key(page), 0)
                > report_generation
            )

        if not await self._async_confirm_primary_state(pixel_effect_matches):
            raise AmaranConnectionError(
                f"{self.name} did not confirm pixel effect {selected.value}"
            )

        # A status query commonly returns only the control page. The control
        # report confirms the preceding app-defined sequence, so retain the
        # exact colour pages we sent while preferring any fields the fixture
        # actually reported.
        reported = self._pixel_state
        merged = expected
        if reported is not None:
            for page in reported.pages:
                if (
                    self._pixel_page_generations.get(_pixel_page_key(page), 0)
                    > report_generation
                ):
                    merged = _merge_pixel_page(merged, page)
        if merged != self._pixel_state:
            self._pixel_state = merged
            self._notify_listeners()

    async def _async_apply_effect2_unlocked(
        self,
        selected: systemfx2.SystemEffect2,
        *,
        intensity: int | None,
    ) -> None:
        """Select a default-safe command-34 effect with the operation lock held."""
        current = self._effect2_state
        active = self.effect_state
        if self._boost_state is not None and self._boost_state.enabled:
            await self._async_set_boost_unlocked(False)
        output_was_on = (
            active.on
            if active is not None
            else self._state is not None and self._state.on
        )
        expected_intensity = (
            intensity
            if intensity is not None
            else (
                active.intensity
                if active is not None
                and active.intensity is not None
                and active.intensity > 0
                else (
                    self._state.intensity
                    if self._state is not None and self._state.intensity > 0
                    else 180
                )
            )
        )
        if current is not None and current.effect is selected:
            payloads = self._effect2_payloads(
                current,
                on=True,
                intensity=expected_intensity,
            )
            expected = self._decode_effect2_sequence(payloads)
            preserving_active_effect = True
        else:
            mode = 1 if self.profile.supports_color else 0
            kelvin = self._default_effect_kelvin()
            payloads = systemfx2.effect2(
                selected,
                intensity=expected_intensity,
                mode=mode,
                kelvin=kelvin,
                gm=(self._preferred_gm + 10) * 10,
                hue=(
                    self._state.hue
                    if self._state is not None and self._state.is_hsi
                    else 1
                ),
                saturation=(
                    self._state.saturation
                    if self._state is not None and self._state.is_hsi
                    else 100
                ),
                center_kelvin=kelvin,
                min_kelvin=self.profile.min_kelvin,
                max_kelvin=self.profile.max_kelvin,
            )
            expected = self._decode_effect2_sequence(payloads)
            preserving_active_effect = False
        for payload in payloads:
            await self._async_send(payload)
        if not output_was_on:
            await self.async_turn_on()

        def effect_matches() -> bool:
            report = self._effect2_state
            if preserving_active_effect:
                return _effect2_state_matches(report, expected)
            return (
                report is not None
                and report.on
                and report.effect is selected
                and intensities_have_same_brightness(
                    report.intensity, expected_intensity
                )
            )

        if not await self._async_confirm_primary_state(effect_matches):
            raise AmaranConnectionError(
                f"{self.name} did not confirm effect {selected.value}"
            )

    async def _async_exit_effect(self, *, intensity: int | None = None) -> None:
        """Leave system-FX mode and restore the last steady light look."""
        current = self.effect_state
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
            target_intensity = (
                intensity if intensity is not None else current.intensity
            ) or 180
            await self.async_set_cct(
                self._default_effect_kelvin(),
                target_intensity,
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
                and self._effect2_state is None
                and self._pixel_state is None
                and self._state is not None
                and self._state.on
                and (
                    target_intensity is None
                    or intensities_have_same_brightness(
                        self._state.intensity, target_intensity
                    )
                )
            )
        ):
            raise AmaranConnectionError(
                f"{self.name} did not confirm leaving its effect"
            )

    async def async_set_effect_frequency(self, frequency: float) -> None:
        """Set the active effect rate, including the APK's value 11=Random."""
        async with self._operation_lock:
            state2 = self._effect2_state
            if state2 is not None:
                if not state2.on or state2.frequency is None:
                    raise AmaranConnectionError(
                        f"{self.name} is not running a rate-adjustable effect"
                    )
                target = max(1, min(11, math.floor(frequency + 0.5)))
                await self._async_update_effect2_unlocked(
                    error_label="rate", frequency=target
                )
                return
            state = self._effect_state
            if state is None or not state.on:
                raise AmaranConnectionError(
                    f"{self.name} is not running an active effect"
                )
            maximum = (
                10
                if state.effect
                in {
                    telink.SystemEffect.CLUB_LIGHTS,
                    telink.SystemEffect.CANDLE,
                    telink.SystemEffect.FIRE,
                    telink.SystemEffect.EXPLOSION,
                    telink.SystemEffect.COLOR_CHASE,
                    telink.SystemEffect.PARTY_LIGHTS,
                }
                else 11
            )
            target = max(1, min(maximum, math.floor(frequency + 0.5)))
            payload = self._effect_payload(state, frequency=target)
            expected = telink.decode_effect(payload)
            assert expected is not None
            await self._async_send(payload)
            if not await self._async_confirm_primary_state(
                lambda: _effect_state_matches(self._effect_state, expected)
            ):
                raise AmaranConnectionError(
                    f"{self.name} did not confirm its effect rate"
                )

    async def async_set_effect_kelvin(self, kelvin: float) -> None:
        """Set the CCT field of an active Ace effect in the app's 50 K steps."""
        async with self._operation_lock:
            state2 = self._effect2_state
            if state2 is not None:
                if not state2.on or state2.kelvin is None:
                    raise AmaranConnectionError(
                        f"{self.name} is not running an active CCT-adjustable effect"
                    )
                target = self.profile.min_kelvin + 50 * math.floor(
                    (kelvin - self.profile.min_kelvin) / 50 + 0.5
                )
                target = min(
                    max(target, self.profile.min_kelvin), self.profile.max_kelvin
                )
                await self._async_update_effect2_unlocked(
                    error_label="colour temperature", kelvin=target
                )
                return
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
                lambda: _effect_state_matches(self._effect_state, expected)
            ):
                raise AmaranConnectionError(
                    f"{self.name} did not confirm its effect colour temperature"
                )

    async def async_set_effect_hue(self, hue: float) -> None:
        """Set the HSI hue carried by an active system effect."""
        async with self._operation_lock:
            state2 = self._effect2_state
            if state2 is not None:
                if not state2.on or state2.hue is None:
                    raise AmaranConnectionError(
                        f"{self.name} is not running an HSI-adjustable effect"
                    )
                target = max(0, min(360, math.floor(hue + 0.5)))
                await self._async_update_effect2_unlocked(error_label="hue", hue=target)
                return
            state = self._effect_state
            if state is None or not state.on or state.hue is None:
                raise AmaranConnectionError(
                    f"{self.name} is not running an HSI-adjustable effect"
                )
            target = max(0, min(360, math.floor(hue + 0.5)))
            payload = self._effect_payload(state, hue=target)
            expected = telink.decode_effect(payload)
            assert expected is not None
            await self._async_send(payload)
            if not await self._async_confirm_primary_state(
                lambda: _effect_state_matches(self._effect_state, expected)
            ):
                raise AmaranConnectionError(
                    f"{self.name} did not confirm its effect hue"
                )

    async def async_set_effect_saturation(self, saturation: float) -> None:
        """Set saturation carried by an active system effect."""
        async with self._operation_lock:
            state2 = self._effect2_state
            if state2 is not None:
                if not state2.on or state2.saturation is None:
                    raise AmaranConnectionError(
                        f"{self.name} is not running a saturation-adjustable effect"
                    )
                target = max(0, min(100, math.floor(saturation + 0.5)))
                await self._async_update_effect2_unlocked(
                    error_label="saturation", saturation=target
                )
                return
            state = self._effect_state
            if state is None or not state.on or state.saturation is None:
                raise AmaranConnectionError(
                    f"{self.name} is not running a saturation-adjustable effect"
                )
            target = max(0, min(100, math.floor(saturation + 0.5)))
            payload = self._effect_payload(state, saturation=target)
            expected = telink.decode_effect(payload)
            assert expected is not None
            await self._async_send(payload)
            if not await self._async_confirm_primary_state(
                lambda: _effect_state_matches(self._effect_state, expected)
            ):
                raise AmaranConnectionError(
                    f"{self.name} did not confirm its effect saturation"
                )

    async def async_set_effect_gm(self, gm: float) -> None:
        """Set the app's G/M field on an active CCT-based system effect."""
        async with self._operation_lock:
            state2 = self._effect2_state
            if state2 is not None:
                if not self.profile.supports_gm or not state2.on or state2.gm is None:
                    raise AmaranConnectionError(
                        f"{self.name} is not running a G/M-adjustable effect"
                    )
                normalized = max(
                    float(self.green_magenta_min),
                    min(float(self.green_magenta_max), gm),
                )
                raw = math.floor((normalized + 10.0) * 10 + 0.5)
                await self._async_update_effect2_unlocked(
                    error_label="green/magenta shift", gm=raw
                )
                return
            state = self._effect_state
            if (
                not self.profile.supports_gm
                or state is None
                or not state.on
                or state.gm is None
            ):
                raise AmaranConnectionError(
                    f"{self.name} is not running a G/M-adjustable effect"
                )
            normalized = max(
                float(self.green_magenta_min),
                min(float(self.green_magenta_max), gm),
            )
            gm_v2 = bool(self.profile.catalog_capabilities.steady_color.gm_v2_version)
            raw = (
                math.floor((normalized + 10.0) * 10 + 0.5)
                if gm_v2
                else 10 * math.floor((normalized + 10.0) + 0.5)
            )
            payload = self._effect_payload(state, gm=raw, gm_flag=gm_v2)
            expected = telink.decode_effect(payload)
            assert expected is not None
            await self._async_send(payload)
            if not await self._async_confirm_primary_state(
                lambda: _effect_state_matches(self._effect_state, expected)
            ):
                raise AmaranConnectionError(
                    f"{self.name} did not confirm its effect green/magenta shift"
                )

    async def async_set_effect_variant(self, option: str) -> None:
        """Set the app-defined colour preset of the active effect."""
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
                lambda: _effect_state_matches(self._effect_state, expected)
            ):
                raise AmaranConnectionError(
                    f"{self.name} did not confirm its effect colour preset"
                )

    async def async_set_effect_color_mode(self, mode: str) -> None:
        """Switch a dual-color legacy effect between CCT and HSI exactly."""
        async with self._operation_lock:
            state = self._effect_state
            if (
                state is None
                or not state.on
                or mode not in self.effect_color_mode_options
            ):
                raise AmaranConnectionError(
                    f"{mode!r} is not available for the active effect"
                )

            target_mode = 1 if mode == EFFECT_COLOR_MODE_HSI else 0
            payload_kwargs: dict[str, float | int | bool] = {"mode": target_mode}
            if target_mode == 1 and state.mode != 1:
                steady = self._state
                payload_kwargs["hue"] = (
                    steady.hue if steady is not None and steady.is_hsi else 0
                )
                payload_kwargs["saturation"] = (
                    steady.saturation if steady is not None and steady.is_hsi else 100
                )
            elif target_mode == 0 and state.mode != 0:
                gm_v2 = bool(
                    self.profile.catalog_capabilities.steady_color.gm_v2_version
                )
                payload_kwargs.update(
                    {
                        "kelvin": self._default_effect_kelvin(),
                        "gm": (
                            math.floor((self._preferred_gm + 10.0) * 10 + 0.5)
                            if gm_v2
                            else 10 * math.floor((self._preferred_gm + 10.0) + 0.5)
                        ),
                        "gm_flag": gm_v2,
                    }
                )

            payload = self._effect_payload(state, **payload_kwargs)
            expected = telink.decode_effect(payload)
            assert expected is not None
            await self._async_send(payload)
            if not await self._async_confirm_primary_state(
                lambda: _effect_state_matches(self._effect_state, expected)
            ):
                raise AmaranConnectionError(
                    f"{self.name} did not confirm its effect color mode"
                )

    async def async_set_boost(self, enabled: bool) -> None:
        """Enter or leave the Ace Boost modal session after a Mesh write."""
        if not self.profile.supports_boost:
            raise AmaranConnectionError(f"Boost is not enabled for {self.name}")
        async with self._operation_lock:
            if self._boost_state is not None and self._boost_state.enabled is enabled:
                return
            await self._async_set_boost_unlocked(enabled)
            await self._async_refresh_state()

    async def _async_set_boost_unlocked(
        self,
        enabled: bool,
        *,
        kelvin: int | None = None,
        gm: int | None = None,
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
        if gm is None:
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

    async def async_set_boost_gm(self, gm: float) -> None:
        """Adjust the Boost dialog's green/magenta value while it is active."""
        if not (self.profile.supports_boost and self.profile.supports_gm):
            raise AmaranConnectionError(f"Boost tint is not enabled for {self.name}")
        async with self._operation_lock:
            state = self._boost_state
            if state is None or not state.enabled:
                raise AmaranConnectionError(
                    f"enable Boost on {self.name} before changing its tint"
                )
            # The original app widget stores M1.0..G1.0 as raw 0..200 with
            # neutral at 100. Home Assistant exposes the familiar -10..+10.
            target = math.floor((gm + 10) * 10 + 0.5)
            target = max(
                (self.green_magenta_min + 10) * 10,
                min((self.green_magenta_max + 10) * 10, target),
            )
            await self._async_set_boost_unlocked(state.enabled, gm=target)

    async def async_set_fan_mode(self, mode: str) -> None:
        """Set a fixture-confirmed fan mode, then query its full report."""
        if mode not in self.profile.fan_modes:
            raise AmaranConnectionError(f"{mode} is not supported by {self.name}")
        selected = telink.FanMode(mode)
        async with self._operation_lock:
            if (
                self._fan_state is not None
                and self._fan_state.mode is selected
                and selected in self._fan_state.supported_modes
            ):
                return
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
            # Manual mode's packet carries the target RPM. Preserve the
            # fixture's latest reported value instead of silently selecting
            # 0 RPM when the mode is re-applied or first chosen.
            fixture_speed = (
                self._fan_state.fixture_speed
                if selected is telink.FanMode.MANUAL and self._fan_state is not None
                else 0
            )
            await self._async_send(telink.fan(selected, fixture_speed))
            # The official app always queries after a fan write. A set packet
            # echo lacks capability and temperature fields and is not enough
            # to confirm that the fixture applied the requested mode.
            await asyncio.sleep(FAN_APPLY_SETTLE)
            for _ in range(2):
                confirmed = await self._async_refresh_optional(
                    telink.fan_request(), self._fan_received
                )
                if (
                    confirmed
                    and self._fan_state is not None
                    and self._fan_state.mode is selected
                    and selected in self._fan_state.supported_modes
                ):
                    return
            raise AmaranConnectionError(f"{self.name} did not confirm fan mode {mode}")

    async def async_set_fan_speed(self, speed_rpm: float) -> None:
        """Set and confirm the target speed while Manual fan mode is active."""
        if telink.FanMode.MANUAL.value not in self.profile.fan_modes:
            raise AmaranConnectionError(
                f"manual fan control is not enabled for {self.name}"
            )
        async with self._operation_lock:
            if self._fan_state is None and not await self._async_refresh_optional(
                telink.fan_request(), self._fan_received
            ):
                raise AmaranConnectionError(f"{self.name} did not report fan state")
            if (
                self._fan_state is None
                or telink.FanMode.MANUAL not in self._fan_state.supported_modes
            ):
                raise AmaranConnectionError(
                    f"manual fan speed is not supported by {self.name}"
                )
            if self._fan_state.mode is not telink.FanMode.MANUAL:
                raise AmaranConnectionError(
                    f"select Manual fan mode on {self.name} before changing its speed"
                )

            target = max(0, min(1000, math.floor(speed_rpm + 0.5)))
            self._fan_received.clear()
            await self._async_send(telink.fan(telink.FanMode.MANUAL, target))
            await asyncio.sleep(FAN_APPLY_SETTLE)
            for _ in range(2):
                confirmed = await self._async_refresh_optional(
                    telink.fan_request(), self._fan_received
                )
                if (
                    confirmed
                    and self._fan_state is not None
                    and self._fan_state.mode is telink.FanMode.MANUAL
                    and self._fan_state.fixture_speed == target
                    and telink.FanMode.MANUAL in self._fan_state.supported_modes
                ):
                    return
            raise AmaranConnectionError(
                f"{self.name} did not confirm manual fan speed {target} RPM"
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
            effect_state = self.effect_state
            expected_gm = self._preferred_gm
            boost_active = self._boost_state is not None and self._boost_state.enabled
            if boost_active and (
                brightness_changed or hs_color is not None or kelvin is not None
            ):
                await self._async_set_boost_unlocked(False)
                boost_active = False
            if self._effect2_state is not None and hs_color is None and kelvin is None:
                await self._async_apply_effect2_unlocked(
                    self._effect2_state.effect,
                    intensity=intensity if brightness_changed else None,
                )
                return
            if self._pixel_state is not None and hs_color is None and kelvin is None:
                if not brightness_changed and self._pixel_state.on is True:
                    # A control-only report after reconnect does not contain
                    # the colour program. Rebuilding an already-on effect
                    # would replace that program with defaults, so an
                    # idempotent HA turn_on performs only a read-back.
                    selected = self._pixel_state.effect
                    if not await self._async_confirm_primary_state(
                        lambda: (
                            self._pixel_state is not None
                            and self._pixel_state.effect is selected
                            and self._pixel_state.on is True
                        )
                    ):
                        raise AmaranConnectionError(
                            f"{self.name} did not confirm its pixel effect state"
                        )
                    return
                if brightness_changed and not _pixel_pages_complete(self._pixel_state):
                    # A status report may contain only the control page. There
                    # is no command-33 brightness-only packet, so rebuilding
                    # here would silently replace unknown custom colours with
                    # defaults. Make the limitation explicit; selecting the
                    # effect again remains the intentional reset path.
                    raise AmaranConnectionError(
                        f"{self.name} has not reported its complete pixel program; "
                        "reselect the effect before changing brightness"
                    )
                await self._async_apply_pixel_effect_unlocked(
                    self._pixel_state.effect,
                    intensity=intensity if brightness_changed else None,
                )
                return
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
                    lambda: _effect_state_matches(self._effect_state, expected_effect)
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
                if (
                    self._effect_state is not None
                    or self._effect2_state is not None
                    or self._pixel_state is not None
                    or current is None
                    or not current.on
                ):
                    return False
                if not intensities_have_same_brightness(
                    current.intensity, intensity
                ) and (
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
                    # The app truncates positive Kelvin to the command-2 wire's
                    # ten-Kelvin resolution before sending it.
                    expected_kelvin = int(kelvin // 10) * 10
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
            pixel_error: AmaranConnectionError | None = None
            pixel_report_generation = self._pixel_report_generation
            if self._boost_state is not None and self._boost_state.enabled:
                try:
                    await self._async_set_boost_unlocked(False)
                except AmaranConnectionError as err:
                    # Safety beats perfect mode bookkeeping: an unavailable
                    # Boost exit write must never prevent the user's OFF.
                    boost_error = err
            if self._pixel_state is not None and self._pixel_state.on:
                try:
                    # Pixel models use their STOP playback state when the app
                    # builds the active effect with EffectState.OFF. Preserve
                    # every reported colour page, then still send the ordinary
                    # power-off command as the safety-critical final action.
                    for payload in _pixel_payloads(
                        self._pixel_state,
                        intensity=None,
                        on=False,
                    ):
                        await self._async_send(payload)
                except AmaranConnectionError as err:
                    pixel_error = err
            await self.async_turn_off()
            power_off_confirmed = await self._async_confirm_primary_state(
                lambda: (
                    (self._effect_state is not None and not self._effect_state.on)
                    or (self._effect2_state is not None and not self._effect2_state.on)
                    or (
                        self._pixel_state is not None
                        and self._pixel_state.on is False
                        and self._pixel_page_generations.get((2, 0), 0)
                        > pixel_report_generation
                    )
                    or (
                        self._effect_state is None
                        and self._effect2_state is None
                        and self._pixel_state is None
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
            if pixel_error is not None:
                raise AmaranConnectionError(
                    f"{self.name} was turned off, but stopping its pixel effect "
                    "could not be sent"
                ) from pixel_error

    async def async_set_gm(self, gm: float) -> None:
        """Set G/M in CCT mode, or remember it while the fixture is in HSI."""
        # Match the protocol's JavaScript-style half-up tie behavior while
        # keeping the cached Number state equal to what the fixture receives.
        gm_v2 = bool(self.profile.catalog_capabilities.steady_color.gm_v2_version)
        rounded = math.floor(gm * 10 + 0.5) / 10 if gm_v2 else math.floor(gm + 0.5)
        target = max(
            self.green_magenta_min,
            min(self.green_magenta_max, rounded),
        )
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
                        and intensities_have_same_brightness(
                            self._state.intensity, state.intensity
                        )
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
        """Probe app-catalog diagnostics independently of primary light state."""
        cataloged = bool(self.profile.app_product_ids)
        if (
            (self.profile.supports_version or cataloged)
            and include_version
            and not await self._async_refresh_optional(
                telink.version_request(), self._version_received
            )
        ):
            _LOGGER.debug("%s did not report version data", self.address)
        if (
            cataloged
            and include_version
            and not await self._async_refresh_optional(
                telink.version2_request(), self._version2_received
            )
        ):
            _LOGGER.debug("%s did not report advanced capability data", self.address)
        # Power is not declared statically by the app. Probe every named model
        # once per connection; only keep polling fixtures that answer.
        if (
            (self.profile.supports_power or cataloged)
            and (include_version or self._power_state is not None)
            and not await self._async_refresh_optional(
                telink.power_request(), self._power_received
            )
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

            candidates = async_mesh_proxy_candidates(
                self.hass,
                self.address,
                net_key=self._net_key,
                unicast_address=self._unicast_address,
                transport_address=self._transport_address,
            )
            if not candidates:
                raise AmaranConnectionError(f"{self.address} is not in range")

            last_error: AmaranConnectionError | None = None
            for candidate in candidates:
                try:
                    client, proxy = await self._async_connect_candidate(candidate)
                except AmaranConnectionError as err:
                    if self._closing:
                        raise
                    last_error = err
                    continue

                if (
                    self._closing
                    or self._client is not client
                    or not client.is_connected
                ):
                    with contextlib.suppress(Exception):
                        await proxy.stop()
                    if self._client is client:
                        self._client = None
                    await self._close_client(client)
                    if self._closing:
                        raise AmaranConnectionError(f"{self.name} is shutting down")
                    last_error = AmaranConnectionError(
                        f"{self.address} disconnected while setting up its mesh proxy"
                    )
                    continue

                self._proxy = proxy
                self._transport_address = candidate.address
                self._reconnect_delay = RECONNECT_MIN_DELAY
                self._missed_polls = 0
                _LOGGER.debug(
                    "connected to %s%s",
                    self.address,
                    " through an alternate BLE address"
                    if self.using_alternate_address
                    else "",
                )
                break
            else:
                if last_error is not None:
                    raise last_error
                raise AmaranConnectionError(f"{self.address} is not in range")

        # Prime the cached state; without a report the entity stays unavailable.
        # Optional report pages are deliberately best-effort: a firmware that
        # omits battery/fan/version replies must not take down basic lighting.
        with contextlib.suppress(AmaranConnectionError):
            await self._async_refresh_state()
        with contextlib.suppress(AmaranConnectionError):
            await self._async_refresh_diagnostics(include_version=True)

    async def _async_connect_candidate(
        self, candidate: MeshProxyCandidate
    ) -> tuple[BleakClient, ProxyClient]:
        """Connect one pre-validated route and install its Mesh proxy transport."""
        _LOGGER.debug(
            "connecting to %s%s",
            self.address,
            " through an alternate BLE address"
            if candidate.address.casefold() != self.address.casefold()
            else "",
        )
        try:
            client = await _async_establish_candidate(
                self.hass,
                candidate,
                self.name,
                self._on_disconnected,
            )
        except (BleakError, TimeoutError) as err:
            raise AmaranConnectionError(
                f"could not connect to {self.address}: {err}"
            ) from err

        if self._closing:
            await self._close_client(client)
            raise AmaranConnectionError(f"{self.name} is shutting down")

        # Advertisement caches are additive: after a factory reset HA can
        # briefly retain the old matching 0x1828 data beside a fresh 0x1827
        # advertisement. Freshly resolved GATT services are authoritative.
        if _client_exposes_only_provisioning_bearer(client):
            await self._close_client(client)
            self._notify_not_provisioned()
            raise AmaranNotProvisionedError(self.address)

        # Install the identity before starting notifications so a disconnect
        # -- or even malformed stored key material rejected by ProxyClient
        # construction -- cannot leak an unowned BLE client or be mistaken for
        # a stale callback from an older connection.
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
                raise AmaranConnectionError(
                    f"{self.address} did not expose the mesh proxy service: {err}"
                ) from err
            if isinstance(
                err,
                (AmaranConnectionError, ProxyError, SequenceExhaustedError),
            ):
                raise AmaranConnectionError(str(err)) from err
            raise

        return client, proxy

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
                self._effect2_state,
                self._pixel_state,
                self._boost_state,
                self._fan_state,
                self._power_state,
                self._version_state,
                self._version2_state,
                self._high_speed_state,
            )
        )
        self._state = None
        self._effect_state = None
        self._effect2_state = None
        self._pixel_state = None
        # Keep the generation monotonic across reconnects. An operation can
        # capture the current value immediately before a send causes a
        # reconnect; resetting it here would make every valid report from the
        # new session look older than that operation.
        self._pixel_page_generations.clear()
        self._boost_state = None
        self._fan_state = None
        self._power_state = None
        self._version_state = None
        self._version2_state = None
        self._high_speed_state = None
        self._missed_polls = 0
        for event in (
            self._state_received,
            self._boost_received,
            self._fan_received,
            self._power_received,
            self._version_received,
            self._version2_received,
            self._high_speed_received,
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
                self._notify_not_provisioned()
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
                if (
                    self._state is not None
                    or self._effect_state is not None
                    or self._effect2_state is not None
                    or self._pixel_state is not None
                )
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

    @callback
    def _notify_not_provisioned(self) -> None:
        """Report loss of mesh membership once per failure episode."""
        if self._not_provisioned_reported:
            return
        self._not_provisioned_reported = True
        self._provisioned_reported = False
        if self._on_not_provisioned is None:
            return
        try:
            self._on_not_provisioned()
        except Exception:
            _LOGGER.exception(
                "not-provisioned callback for %s failed",
                self.address,
            )

    @callback
    def _notify_provisioned(self) -> None:
        """Report one recovery proven by an authenticated primary report."""
        if self._provisioned_reported:
            return
        if self._on_provisioned is None:
            self._provisioned_reported = True
            self._not_provisioned_reported = False
            return
        try:
            self._on_provisioned()
        except Exception:
            _LOGGER.exception(
                "provisioned callback for %s failed",
                self.address,
            )
            return
        self._provisioned_reported = True
        self._not_provisioned_reported = False

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
        # Bit 79 is the proprietary write flag for the common command family.
        # Command 53 is exceptional: its app builder emits operation 0 and its
        # exact layout also permits operation 1. ProxyClient filters our own
        # relayed network packet, so accept either command-53 form while still
        # refusing write-form echoes for every other command.
        if len(message.parameters) != 10:
            return
        command = message.parameters[9] & 0x7F
        if message.parameters[9] & 0x80 and command != highspeed.CMD_HIGH_SPEED:
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
            report = systemfx2.decode_report2(message.parameters)
        if report is None:
            report = pixelfx.decode(message.parameters)
        if report is None:
            report = highspeed.decode_high_speed(message.parameters)
        if report is None:
            return

        changed = False
        primary_report = False
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
                or self._effect2_state is not None
                or self._pixel_state is not None
                or self._boost_state != previous_boost
                or self._preferred_gm != previous_gm
            )
            self._state = report
            self._effect_state = None
            self._effect2_state = None
            self._pixel_state = None
            self._pixel_page_generations.clear()
            self._missed_polls = 0
            self._state_received.set()
            primary_report = True
        elif isinstance(report, telink.EffectState):
            if report.effect.value not in self.profile.effects:
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
                new_effect != self._effect_state
                or self._effect2_state is not None
                or self._pixel_state is not None
                or self._boost_state != previous_boost
            )
            self._effect_state = new_effect
            self._effect2_state = None
            self._pixel_state = None
            self._pixel_page_generations.clear()
            self._missed_polls = 0
            self._state_received.set()
            primary_report = True
        elif isinstance(report, systemfx2.SystemEffect2State):
            if report.effect.value not in self.profile.system_effects2:
                return
            previous_boost = self._boost_state
            merged = systemfx2.merge_effect2_states(self._effect2_state, report)
            if self.profile.supports_boost and self._boost_state is None:
                minimum = self.profile.boost_min_kelvin or self.profile.min_kelvin
                maximum = self.profile.boost_max_kelvin or self.profile.max_kelvin
                self._boost_state = telink.BoostState(
                    False,
                    min(max(self._default_effect_kelvin(), minimum), maximum),
                    100,
                )
            changed = (
                merged != self._effect2_state
                or self._effect_state is not None
                or self._pixel_state is not None
                or self._boost_state != previous_boost
            )
            self._effect2_state = merged
            self._effect_state = None
            self._pixel_state = None
            self._pixel_page_generations.clear()
            self._missed_polls = 0
            self._state_received.set()
            primary_report = True
        elif isinstance(report, pixelfx.PixelEffectState):
            if report.effect.value not in self.profile.pixel_effects:
                return
            if (
                self._pixel_state is None
                or self._pixel_state.effect is not report.effect
            ):
                self._pixel_page_generations.clear()
            self._pixel_report_generation += 1
            self._pixel_page_generations[_pixel_page_key(report)] = (
                self._pixel_report_generation
            )
            merged = _merge_pixel_page(self._pixel_state, report)
            changed = (
                merged != self._pixel_state
                or self._effect_state is not None
                or self._effect2_state is not None
            )
            self._pixel_state = merged
            self._effect_state = None
            self._effect2_state = None
            self._missed_polls = 0
            self._state_received.set()
            primary_report = True
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
            if not (self.profile.supports_power or self.profile.app_product_ids):
                return
            changed = report != self._power_state
            self._power_state = report
            self._power_received.set()
        elif isinstance(report, telink.VersionState):
            if not (self.profile.supports_version or self.profile.app_product_ids):
                return
            changed = report != self._version_state
            self._version_state = report
            self._version_received.set()
        elif isinstance(report, telink.Version2State):
            if not self.profile.app_product_ids:
                return
            changed = report != self._version2_state
            self._version2_state = report
            self._version2_received.set()
        elif isinstance(report, highspeed.HighSpeedMessage):
            if not self.profile.catalog_capabilities.high_speed_photography.supported:
                return
            changed = report != self._high_speed_state
            self._high_speed_state = report
            self._high_speed_received.set()

        # ProxyClient delivered this only after decrypting the private NetKey
        # and AppKey, and the accepted source is this node's unicast address.
        # Optional report pages are deliberately insufficient: require one of
        # the primary state families that makes the light entity available.
        if primary_report:
            self._notify_provisioned()

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
    transport_address: str | None = None,
) -> bool:
    """Factory-reset the node so it stops belonging to our mesh.

    Without this, deleting the config entry would throw away the only copy of
    the keys and strand the fixture in a network nothing can talk to -- the
    owner would have to reset it by hand before Home Assistant or the amaran
    app could adopt it again.
    """
    candidates = async_mesh_proxy_candidates(
        hass,
        address,
        net_key=net_key,
        unicast_address=unicast_address,
        transport_address=transport_address,
    )
    if not candidates:
        return False

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
    last_error: BaseException | None = None
    for candidate in candidates:
        try:
            client = await _async_establish_candidate(hass, candidate, address)
        except (BleakError, TimeoutError) as err:
            last_error = err
            continue
        proxy: ProxyClient | None = None
        try:
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
            reset_acknowledged = await ConfigClient(proxy, unicast_address).node_reset()
            # Many nodes reset before their status reaches the proxy. In that
            # case a link drop positively confirms the command.
            for _ in range(30):
                if not client.is_connected:
                    break
                await asyncio.sleep(0.1)
            if reset_acknowledged or not client.is_connected:
                return True
        except (BleakError, OSError, TimeoutError, ProxyError) as err:
            last_error = err
        finally:
            if proxy is not None:
                with contextlib.suppress(Exception):
                    await proxy.stop()
            with contextlib.suppress(Exception, TimeoutError):
                async with asyncio.timeout(DISCONNECT_TIMEOUT):
                    await client.disconnect()

    if last_error is not None:
        raise last_error
    return False


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
    transport_address: str | None = None,
) -> int:
    """Resume post-provision configuration retained in a config entry."""
    candidates = async_mesh_proxy_candidates(
        hass,
        address,
        net_key=net_key,
        unicast_address=unicast_address,
        transport_address=transport_address,
    )
    if not candidates:
        raise AmaranConnectionError(f"{address} is not in range")

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
    last_error: AmaranConnectionError | NodeConfigurationError | None = None
    for candidate in candidates:
        try:
            client = await _async_establish_candidate(hass, candidate, name)
        except (BleakError, TimeoutError) as err:
            last_error = AmaranConnectionError(f"could not connect to {address}: {err}")
            continue
        try:
            if _client_exposes_only_provisioning_bearer(client):
                raise AmaranNotProvisionedError(address)
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
        except NodeConfigurationError as err:
            last_error = err
        finally:
            with contextlib.suppress(Exception, TimeoutError):
                async with asyncio.timeout(DISCONNECT_TIMEOUT):
                    await client.disconnect()

    assert last_error is not None
    raise last_error
