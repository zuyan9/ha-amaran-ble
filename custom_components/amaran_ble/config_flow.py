"""Config flow for amaran BLE.

Adding a fixture provisions it into a private Bluetooth Mesh network that this
integration creates and owns. The keys never leave Home Assistant, and nothing
has to be extracted from amaran's own app.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Mapping
from enum import Enum, auto
from typing import Any

import voluptuous as vol
from bleak import BleakClient
from bleak.exc import BleakError
from bleak_retry_connector import establish_connection
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothCallbackMatcher,
    BluetoothCallbackReplay,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
)
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector

from .amaranble import crypto
from .amaranble.gatt import (
    MESH_PROVISIONING_SERVICE,
    MESH_PROXY_SERVICE,
    PROVISIONING_DATA_IN,
    PROVISIONING_DATA_OUT,
    PROXY_DATA_IN,
    PROXY_DATA_OUT,
)
from .amaranble.network import NetworkKeys
from .amaranble.provisioning import Provisioner, ProvisioningError
from .amaranble.telink import MAX_KELVIN, MIN_KELVIN
from .const import (
    CONF_APP_KEY,
    CONF_DEVICE_KEY,
    CONF_INITIAL_SEQUENCE,
    CONF_IV_INDEX,
    CONF_LOCAL_ADDRESS,
    CONF_MAX_KELVIN,
    CONF_MIN_KELVIN,
    CONF_MODEL,
    CONF_NEEDS_CONFIGURATION,
    CONF_NET_KEY,
    CONF_NUM_ELEMENTS,
    CONF_SEQUENCE_STORE_ID,
    CONF_SUPPORTS_CCT,
    CONF_SUPPORTS_COLOR,
    CONF_SUPPORTS_GM,
    CONF_TRANSPORT_ADDRESS,
    CONF_UNICAST_ADDRESS,
    DEFAULT_MAX_KELVIN,
    DEFAULT_MIN_KELVIN,
    DEFAULT_PROFILE,
    DEFAULT_SUPPORTS_CCT,
    DEFAULT_SUPPORTS_COLOR,
    DEFAULT_SUPPORTS_GM,
    DOMAIN,
    NODE_ADDRESS,
    PROFILE_GENERIC,
    PROVISIONER_ADDRESS,
    TELINK_ADDRESS_PREFIX,
)
from .pending import (
    PendingProvisionError,
    async_get_pending,
    async_get_pending_records,
    async_save_pending,
)
from .profiles import CATALOG_PROFILES, get_fixture_profile
from .reconfiguration import (
    async_reprovision_candidates,
    async_update_reprovisioned_entry,
)

_LOGGER = logging.getLogger(__name__)

# A broken BLE backend must not leave a config flow stuck while it tears down
# the provisioning connection.
DISCONNECT_TIMEOUT = 5.0

# Recovery service discovery is intentionally uncached. Keep its retry window
# bounded so an unreachable fixture returns control to the config flow while
# retaining the only credentials that may still own it.
RECOVERY_PROBE_TIMEOUT = 30.0
RECOVERY_PROBE_ATTEMPTS = 2
RECOVERY_IDENTITY_TIMEOUT = 15.0
RECOVERY_ACTIVE_SCAN_DURATION = 5.0


class _MeshBearer(Enum):
    """Mutually exclusive live GATT bearers exposed by a Mesh node."""

    PROVISIONING = auto()
    PROXY = auto()


class _PendingAction(Enum):
    """How a durable record should be consumed after live resolution."""

    RECOVER = auto()
    REPROVISION = auto()


def is_amaran_fixture(info: BluetoothServiceInfoBleak) -> bool:
    """Decide whether a mesh device is one of ours.

    Manufacturer data alone is not enough to go on: amaran fixtures put it in
    the scan response, and Home Assistant's Bluetooth proxies commonly run
    passive-only scans that never request one. The primary advertisement
    carries just the mesh service UUID and its service data, so the address is
    the only fixture-specific field guaranteed to be present -- amaran fixtures
    use Telink's A4:C1:38 prefix. Active advertisements are also accepted by
    their amaran, Aputure, Sidus, or model name so newer fixtures are not tied
    to one chip-vendor address range. The stock "SLCK" name alone is not a
    brand signal; unrelated Telink Mesh products use it too.
    """
    if not (
        MESH_PROVISIONING_SERVICE in info.service_data
        or MESH_PROXY_SERVICE in info.service_data
    ):
        return False
    name = (info.name or "").casefold()
    return info.address.upper().startswith(TELINK_ADDRESS_PREFIX) or any(
        marker in name for marker in ("amaran", "aputure", "sidus", "150c")
    )


def _kelvin_selector() -> selector.NumberSelector:
    """A slider over the range the wire format can actually carry."""
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=MIN_KELVIN, max=MAX_KELVIN, step=100, unit_of_measurement="K"
        )
    )


def _model_selector() -> selector.SelectSelector:
    """Offer the app's catalog plus a manually configurable fallback."""

    def label(profile: Any) -> str:
        name = profile.name
        if profile.manufacturer and not name.casefold().startswith(
            profile.manufacturer.casefold()
        ):
            name = f"{profile.manufacturer} {name}"
        return f"{name} ({'hardware-tested' if profile.hardware_tested else 'experimental'})"

    catalog = sorted(
        CATALOG_PROFILES,
        key=lambda profile: (
            not profile.hardware_tested,
            profile.manufacturer.casefold(),
            profile.name.casefold(),
        ),
    )
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[
                selector.SelectOptionDict(
                    value=PROFILE_GENERIC, label="Generic amaran light"
                ),
                *(
                    selector.SelectOptionDict(value=profile.key, label=label(profile))
                    for profile in catalog
                ),
            ],
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


def options_for_profile(values: dict[str, Any]) -> dict[str, Any]:
    """Normalize form values into a safe, complete capability selection."""
    requested_model = values.get(CONF_MODEL, DEFAULT_PROFILE)
    profile = get_fixture_profile(
        requested_model if isinstance(requested_model, str) else None
    )
    model = profile.key
    if model != PROFILE_GENERIC:
        return {
            CONF_MODEL: model,
            CONF_SUPPORTS_CCT: profile.supports_cct,
            CONF_SUPPORTS_COLOR: profile.supports_color,
            CONF_SUPPORTS_GM: profile.supports_gm,
            CONF_MIN_KELVIN: profile.min_kelvin,
            CONF_MAX_KELVIN: profile.max_kelvin,
        }

    supports_cct = bool(values.get(CONF_SUPPORTS_CCT, DEFAULT_SUPPORTS_CCT))
    return {
        CONF_MODEL: model,
        CONF_SUPPORTS_CCT: supports_cct,
        CONF_SUPPORTS_COLOR: bool(
            values.get(CONF_SUPPORTS_COLOR, DEFAULT_SUPPORTS_COLOR)
        ),
        CONF_SUPPORTS_GM: supports_cct
        and bool(values.get(CONF_SUPPORTS_GM, DEFAULT_SUPPORTS_GM)),
        CONF_MIN_KELVIN: int(values.get(CONF_MIN_KELVIN, DEFAULT_MIN_KELVIN)),
        CONF_MAX_KELVIN: int(values.get(CONF_MAX_KELVIN, DEFAULT_MAX_KELVIN)),
    }


def capability_errors(values: dict[str, Any]) -> dict[str, str]:
    """Validate the editable capability fields of the generic profile."""
    requested_model = values.get(CONF_MODEL, DEFAULT_PROFILE)
    if (
        get_fixture_profile(
            requested_model if isinstance(requested_model, str) else None
        ).key
        != PROFILE_GENERIC
    ):
        return {}
    supports_cct = bool(values.get(CONF_SUPPORTS_CCT, DEFAULT_SUPPORTS_CCT))
    if values.get(CONF_SUPPORTS_GM, DEFAULT_SUPPORTS_GM) and not supports_cct:
        return {CONF_SUPPORTS_GM: "gm_requires_cct"}
    if values.get(CONF_SUPPORTS_COLOR, DEFAULT_SUPPORTS_COLOR) and not supports_cct:
        return {CONF_SUPPORTS_COLOR: "color_requires_cct"}
    if supports_cct and values.get(CONF_MIN_KELVIN, DEFAULT_MIN_KELVIN) >= values.get(
        CONF_MAX_KELVIN, DEFAULT_MAX_KELVIN
    ):
        return {CONF_MAX_KELVIN: "invalid_range"}
    return {}


def _options_form_values(entry: ConfigEntry) -> dict[str, Any]:
    """Return canonical form values without losing a legacy data model."""
    values = dict(entry.options)
    requested_model = values.get(
        CONF_MODEL, entry.data.get(CONF_MODEL, DEFAULT_PROFILE)
    )
    profile = get_fixture_profile(
        requested_model if isinstance(requested_model, str) else None
    )
    if profile.key != PROFILE_GENERIC:
        values.update(options_for_profile({CONF_MODEL: profile.key}))
    else:
        values[CONF_MODEL] = PROFILE_GENERIC
    return values


def suggested_title(info: BluetoothServiceInfoBleak) -> str:
    """Name the fixture after its address suffix, which is printed on the unit.

    The advertised name is rarely usable: a passive scan yields no name at all
    (Home Assistant then substitutes the address), and an active one returns
    the Telink stack's stock "SLCK Light", which is the same on every fixture.
    """
    plain_address = info.address.replace(":", "").upper()
    name = (info.name or "").strip()
    if (
        not name
        or name.replace(":", "").upper() == plain_address
        or name.upper().startswith("SLCK")
    ):
        name = "amaran light"
    return f"{name} ({plain_address[-6:]})"


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


def _stored_proxy_identity_matches(data: Mapping[str, Any], proxy_data: bytes) -> bool:
    """Return whether stored network data authenticates one Proxy page."""
    try:
        net_key_value = data[CONF_NET_KEY]
        net_key = bytes.fromhex(net_key_value)
        unicast_address = data.get(CONF_UNICAST_ADDRESS, NODE_ADDRESS)
    except KeyError, TypeError, ValueError:
        return False
    if (
        not isinstance(net_key_value, str)
        or len(net_key) != 16
        or not isinstance(unicast_address, int)
        or isinstance(unicast_address, bool)
        or not 0x0001 <= unicast_address <= 0x7FFF
    ):
        return False
    return (
        NetworkKeys.derive(net_key).proxy_identity_match(proxy_data, unicast_address)
        is not None
    )


def _entry_proxy_identity_matches(entry: ConfigEntry, proxy_data: bytes) -> bool:
    """Return whether a current config entry already owns a Proxy page."""
    return _stored_proxy_identity_matches(entry.data, proxy_data)


def _pending_proxy_matches(
    records: Mapping[str, Mapping[str, Any]],
    proxy_data: bytes,
    configured_addresses: set[str],
) -> tuple[str, ...]:
    """Return orphaned stable pending keys authenticated by one Proxy page."""
    matches: list[str] = []
    for address, record in records.items():
        if address.casefold() in configured_addresses:
            continue
        data = record.get("data")
        if isinstance(data, Mapping) and _stored_proxy_identity_matches(
            data, proxy_data
        ):
            matches.append(address)
    return tuple(matches)


async def _async_disconnect(client: BleakClient) -> None:
    """Bound best-effort teardown without hiding the operation's result."""
    with contextlib.suppress(Exception, TimeoutError):
        async with asyncio.timeout(DISCONNECT_TIMEOUT):
            await client.disconnect()


def _has_characteristics(service: Any, *characteristic_uuids: str) -> bool:
    """Return whether one resolved service has its complete Mesh bearer."""
    return service is not None and all(
        service.get_characteristic(uuid) is not None for uuid in characteristic_uuids
    )


async def _async_probe_mesh_bearer(
    hass: HomeAssistant, info: BluetoothServiceInfoBleak
) -> _MeshBearer:
    """Resolve the fixture's current bearer from a fresh GATT connection.

    Home Assistant and scanner backends merge advertisement fields over time,
    so even a newly delivered advertisement may contain a bearer UUID retained
    from before a provisioning transition. GATT service discovery with Bleak's
    service cache disabled is the active observation used for this destructive
    recovery decision.
    """
    ble_device = bluetooth.async_ble_device_from_address(
        hass, info.address, connectable=True
    )
    if ble_device is None:
        raise BleakError(f"{info.address} is no longer in range")

    name = f"{suggested_title(info)} recovery probe"
    async with asyncio.timeout(RECOVERY_PROBE_TIMEOUT):
        client = await establish_connection(
            BleakClient,
            ble_device,
            name,
            max_attempts=RECOVERY_PROBE_ATTEMPTS,
            use_services_cache=False,
        )

    try:
        services = client.services
        provisioning_service = services.get_service(MESH_PROVISIONING_SERVICE)
        proxy_service = services.get_service(MESH_PROXY_SERVICE)
        provisioning = _has_characteristics(
            provisioning_service, PROVISIONING_DATA_IN, PROVISIONING_DATA_OUT
        )
        proxy = _has_characteristics(proxy_service, PROXY_DATA_IN, PROXY_DATA_OUT)

        # A partial known service, both mutually exclusive services, or neither
        # service could be a transitioning fixture or stale backend database.
        # None is strong enough evidence to throw away the pending mesh keys.
        incomplete = (provisioning_service is not None and not provisioning) or (
            proxy_service is not None and not proxy
        )
        if incomplete or provisioning == proxy:
            raise BleakError(f"{info.address} exposed an ambiguous Mesh GATT bearer")
        return _MeshBearer.PROVISIONING if provisioning else _MeshBearer.PROXY
    finally:
        await _async_disconnect(client)


async def _async_verify_proxy_identity(
    hass: HomeAssistant,
    info: BluetoothServiceInfoBleak,
    network_keys: NetworkKeys,
    unicast_address: int,
) -> None:
    """Wait for a fresh Proxy identity page authenticated by the pending NetKey."""
    matched = asyncio.get_running_loop().create_future()

    @callback
    def advertisement_received(
        service_info: BluetoothServiceInfoBleak, _change: Any
    ) -> None:
        if matched.done():
            return
        service_data = _service_data(service_info, MESH_PROXY_SERVICE)
        if service_data is not None and network_keys.proxy_identity_match(
            service_data, unicast_address
        ):
            matched.set_result(None)

    cancel = bluetooth.async_register_callback(
        hass,
        advertisement_received,
        BluetoothCallbackMatcher(address=info.address),
        BluetoothScanningMode.ACTIVE,
        replay=BluetoothCallbackReplay.DISABLED,
    )
    try:
        # Scanner and manager histories deliberately merge mutually exclusive
        # Mesh fields. Clearing them before an on-demand active sweep makes the
        # callback a current page; only a cryptographic match can complete it.
        bluetooth.async_clear_advertisement_history(hass, info.address)
        try:
            async with asyncio.timeout(RECOVERY_IDENTITY_TIMEOUT):
                await bluetooth.async_request_active_scan(
                    hass, duration=RECOVERY_ACTIVE_SCAN_DURATION
                )
                await matched
        except TimeoutError as err:
            raise BleakError(
                f"{info.address} did not advertise an identity for the pending mesh"
            ) from err
    finally:
        cancel()


async def _async_recover_pending(
    hass: HomeAssistant,
    info: BluetoothServiceInfoBleak,
    record: dict[str, Any],
    *,
    pending_address: str | None = None,
    require_proxy_identity: bool = False,
) -> tuple[dict[str, Any], _PendingAction]:
    """Recover committed keys, or prove old uncommitted keys safe to replace."""
    if record.get("committed") is True and not require_proxy_identity:
        bearer: _MeshBearer | None = None
    else:
        bearer = await _async_probe_mesh_bearer(hass, info)
    try:
        pending = dict(record["data"])
    except (TypeError, ValueError) as err:
        raise PendingProvisionError(
            "pending provisioning credentials have an invalid data record"
        ) from err
    updated = dict(record)
    if CONF_SEQUENCE_STORE_ID not in pending:
        # Older records predate stable sequence-store IDs. Persist the upgrade
        # before a replacement config entry can consume any mesh sequence.
        pending[CONF_SEQUENCE_STORE_ID] = crypto.random_bytes(16).hex()
        updated["data"] = pending
    if require_proxy_identity and bearer is not _MeshBearer.PROXY:
        raise BleakError(
            f"{info.address} no longer exposes the Proxy identity that matched "
            "the orphaned provisioning record"
        )
    if bearer is _MeshBearer.PROXY and (
        require_proxy_identity or updated.get("committed") is not True
    ):
        # A Proxy service only proves that some mesh owns the node. Require a
        # fresh Network ID or Node Identity page authenticated by our pending
        # NetKey before promoting credentials whose DATA result was uncertain.
        unicast_address = pending.get(CONF_UNICAST_ADDRESS, NODE_ADDRESS)
        if (
            not isinstance(unicast_address, int)
            or isinstance(unicast_address, bool)
            or not 0x0001 <= unicast_address <= 0x7FFF
        ):
            raise PendingProvisionError(
                "pending provisioning credentials have an invalid unicast address"
            )
        await _async_verify_proxy_identity(
            hass,
            info,
            NetworkKeys.derive(_decode_pending_key(pending, CONF_NET_KEY)),
            unicast_address,
        )
        updated["committed"] = True
    if updated != record:
        await async_save_pending(hass, pending_address or info.address, updated)

    if bearer is _MeshBearer.PROVISIONING:
        # Retain the prepared network credentials until a replacement DeviceKey
        # is saved by the next pre-DATA hook. If HA stops before that hook, this
        # same idempotent recovery path can safely run again.
        _LOGGER.info(
            "retrying interrupted pre-DATA provisioning for %s with its durable "
            "network credentials",
            info.address,
        )
        return pending, _PendingAction.REPROVISION

    _LOGGER.info(
        "recovering previously provisioned fixture %s from durable credentials",
        info.address,
    )
    return pending, _PendingAction.RECOVER


def _decode_pending_key(data: dict[str, Any], key: str) -> bytes:
    """Decode one prepared 128-bit key without losing the record on failure."""
    try:
        value = data[key]
        decoded = bytes.fromhex(value)
    except (KeyError, TypeError, ValueError) as err:
        raise PendingProvisionError(
            f"pending provisioning credentials contain an invalid {key}"
        ) from err
    if len(decoded) != 16:
        raise PendingProvisionError(
            f"pending provisioning credentials contain an invalid {key}"
        )
    return decoded


async def async_provision_fixture(
    hass: HomeAssistant,
    info: BluetoothServiceInfoBleak,
    *,
    _force_reprovision: bool = False,
    _recovery_address: str | None = None,
    _prepared_data: Mapping[str, Any] | None = None,
    _require_proxy_identity: bool = False,
) -> dict[str, Any]:
    """Provision one fixture or recover a crash-interrupted provisioning."""
    pending_address = _recovery_address or info.address
    prepared: dict[str, Any] | None = None
    pending_record = await async_get_pending(hass, pending_address)
    if _require_proxy_identity and (
        pending_record is None or "data" not in pending_record
    ):
        raise PendingProvisionError(
            "the authenticated provisioning recovery record is no longer available"
        )
    if pending_record is not None and "data" in pending_record:
        if _force_reprovision:
            bearer = await _async_probe_mesh_bearer(hass, info)
            try:
                prepared = dict(pending_record["data"])
            except (TypeError, ValueError) as err:
                raise PendingProvisionError(
                    "pending provisioning credentials have an invalid data record"
                ) from err
            updated = dict(pending_record)
            if CONF_SEQUENCE_STORE_ID not in prepared:
                prepared[CONF_SEQUENCE_STORE_ID] = crypto.random_bytes(16).hex()
            if bearer is _MeshBearer.PROXY:
                original_device_key = (
                    _prepared_data.get(CONF_DEVICE_KEY)
                    if _prepared_data is not None
                    else None
                )
                replacement_device_key = prepared.get(CONF_DEVICE_KEY)
                if (
                    not isinstance(original_device_key, str)
                    or not isinstance(replacement_device_key, str)
                    or replacement_device_key == original_device_key
                ):
                    # The lifetime recovery copy for a normally configured
                    # entry contains the same DeviceKey. Only the fresh key
                    # written by a repair's pre-DATA hook proves this Proxy is
                    # a repair transaction that HA can safely resume.
                    raise BleakError(
                        f"{info.address} is a Mesh Proxy without an interrupted "
                        "re-provisioning transaction"
                    )
                unicast_address = prepared.get(CONF_UNICAST_ADDRESS, NODE_ADDRESS)
                if (
                    not isinstance(unicast_address, int)
                    or isinstance(unicast_address, bool)
                    or not 0x0001 <= unicast_address <= 0x7FFF
                ):
                    raise PendingProvisionError(
                        "pending provisioning credentials have an invalid unicast address"
                    )
                await _async_verify_proxy_identity(
                    hass,
                    info,
                    NetworkKeys.derive(_decode_pending_key(prepared, CONF_NET_KEY)),
                    unicast_address,
                )
                # This is either a false reset report or a retry after the
                # previous repair crossed Provisioning Data/Complete but HA
                # stopped before updating the config entry. The authenticated
                # proxy proves both the credentials and alternate route.
                prepared[CONF_ADDRESS] = info.address
                prepared[CONF_NAME] = suggested_title(info)
                updated["data"] = prepared
                updated["committed"] = True
                if updated != pending_record:
                    await async_save_pending(hass, pending_address, updated)
                return prepared

            updated["data"] = prepared
            # Once the live fixture is proven reset, Completion from the old
            # transaction no longer describes it. Durably downgrade the record
            # before beginning a new handshake so a crash retries this path.
            updated["committed"] = False
            if updated != pending_record:
                await async_save_pending(hass, pending_address, updated)
        else:
            pending, action = await _async_recover_pending(
                hass,
                info,
                pending_record,
                pending_address=pending_address,
                require_proxy_identity=_require_proxy_identity,
            )
            if action is _PendingAction.RECOVER:
                if _recovery_address is not None:
                    # The pending-record key predates the config entry and is
                    # therefore its durable identity. Record the newly proven
                    # BLE route separately instead of replacing entity IDs with
                    # a random transport address.
                    pending[CONF_ADDRESS] = pending_address
                    if info.address.casefold() == pending_address.casefold():
                        pending.pop(CONF_TRANSPORT_ADDRESS, None)
                    else:
                        pending[CONF_TRANSPORT_ADDRESS] = info.address
                    updated = {
                        **pending_record,
                        "data": pending,
                        "committed": True,
                    }
                    await async_save_pending(hass, pending_address, updated)
                return pending
            prepared = pending
    elif _force_reprovision:
        bearer = await _async_probe_mesh_bearer(hass, info)
        if bearer is _MeshBearer.PROXY:
            # A repair that completed always has a pre-DATA pending record. A
            # proxy without one cannot supply the fresh DeviceKey and is not a
            # recoverable replacement for this entry.
            raise BleakError(
                f"{info.address} is a Mesh Proxy without recovery credentials"
            )
        if _prepared_data is not None:
            prepared = dict(_prepared_data)
            if CONF_SEQUENCE_STORE_ID not in prepared:
                prepared[CONF_SEQUENCE_STORE_ID] = crypto.random_bytes(16).hex()
            await async_save_pending(
                hass,
                pending_address,
                {"data": prepared, "committed": False},
            )

    if prepared is None:
        net_key = crypto.random_bytes(16)
        app_key = crypto.random_bytes(16)
        # Config entries are written asynchronously. If Home Assistant crashes
        # after setup has reserved mesh sequences but before the entry reaches
        # disk, recovery creates a new entry_id. Keep the sequence store tied
        # to the durable pending credentials instead of that transient ID.
        sequence_store_id = crypto.random_bytes(16).hex()
    else:
        net_key = _decode_pending_key(prepared, CONF_NET_KEY)
        app_key = _decode_pending_key(prepared, CONF_APP_KEY)
        sequence_store_id = prepared.get(CONF_SEQUENCE_STORE_ID)
        if not isinstance(sequence_store_id, str) or not sequence_store_id:
            raise PendingProvisionError(
                "pending provisioning credentials have an invalid sequence store ID"
            )
    name = suggested_title(info)
    data: dict[str, Any] | None = None

    async def save_before_commit(device_key: bytes, num_elements: int) -> None:
        """Save everything needed to recover before the irreversible PDU."""
        nonlocal data
        data = {
            CONF_ADDRESS: info.address,
            CONF_NAME: name,
            CONF_NET_KEY: net_key.hex(),
            CONF_APP_KEY: app_key.hex(),
            CONF_DEVICE_KEY: device_key.hex(),
            CONF_UNICAST_ADDRESS: NODE_ADDRESS,
            CONF_LOCAL_ADDRESS: PROVISIONER_ADDRESS,
            CONF_NUM_ELEMENTS: num_elements,
            CONF_IV_INDEX: 0,
            CONF_INITIAL_SEQUENCE: 0,
            CONF_SEQUENCE_STORE_ID: sequence_store_id,
            CONF_NEEDS_CONFIGURATION: True,
        }
        await async_save_pending(
            hass,
            pending_address,
            {"data": data, "committed": False},
        )

    ble_device = bluetooth.async_ble_device_from_address(
        hass, info.address, connectable=True
    )
    if ble_device is None:
        raise BleakError(f"{info.address} is no longer in range")

    client = await establish_connection(BleakClient, ble_device, name)
    try:
        await Provisioner(client).provision(
            network_key=net_key,
            unicast_address=NODE_ADDRESS,
            iv_index=0,
            before_commit=save_before_commit,
        )
    finally:
        await _async_disconnect(client)

    if data is None:
        raise ProvisioningError(
            "provisioning completed without saving recovery credentials"
        )
    # Mark the unambiguously successful case. If Home Assistant stopped
    # between Provisioning Data and Complete, a retry instead proves the
    # committed state from a fresh live GATT Proxy bearer.
    await async_save_pending(
        hass,
        pending_address,
        {"data": data, "committed": True},
    )
    return data


async def async_reprovision_fixture(
    hass: HomeAssistant,
    info: BluetoothServiceInfoBleak,
    *,
    recovery_address: str | None = None,
    prepared_data: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Reprovision, or recover a completed repair, under one stable record."""
    return await async_provision_fixture(
        hass,
        info,
        _force_reprovision=True,
        _recovery_address=recovery_address,
        _prepared_data=prepared_data,
    )


class AmaranConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle discovery, provisioning and re-provisioning."""

    VERSION = 1
    MINOR_VERSION = 2

    def __init__(self) -> None:
        self._discovery: BluetoothServiceInfoBleak | None = None
        self._discovered: dict[str, BluetoothServiceInfoBleak] = {}
        self._recovery_address: str | None = None
        self._recovery_addresses: dict[str, str] = {}

    def _proxy_identity_context(
        self,
        info: BluetoothServiceInfoBleak,
        pending_records: Mapping[str, Mapping[str, Any]],
    ) -> tuple[ConfigEntry | None, str | None, bool]:
        """Resolve a Proxy page to a configured entry or one orphaned record."""
        proxy_data = _service_data(info, MESH_PROXY_SERVICE)
        if proxy_data is None:
            return None, None, False

        entries = self._async_current_entries(include_ignore=False)
        if owner := next(
            (
                entry
                for entry in entries
                if _entry_proxy_identity_matches(entry, proxy_data)
            ),
            None,
        ):
            return owner, None, False

        configured_addresses = {
            value.casefold()
            for entry in entries
            for value in (entry.unique_id, entry.data.get(CONF_ADDRESS))
            if isinstance(value, str)
        }
        matches = _pending_proxy_matches(
            pending_records, proxy_data, configured_addresses
        )
        if len(matches) == 1:
            return None, matches[0], False
        if len(matches) > 1:
            _LOGGER.error(
                "refusing ambiguous Proxy recovery for %s: %d durable records match",
                info.address,
                len(matches),
            )
            return None, None, True

        # A direct pending record with a non-matching Proxy identity indicates
        # address reuse or storage corruption. Never let the normal offline
        # committed-record path adopt it solely because the MAC is equal.
        direct_record = next(
            (
                record
                for address, record in pending_records.items()
                if address.casefold() == info.address.casefold()
            ),
            None,
        )
        return None, None, direct_record is not None

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        records = await async_get_pending_records(self.hass)
        owner, recovery_address, unsafe = self._proxy_identity_context(
            discovery_info, records
        )
        if owner is not None:
            if owner.unique_id is None:
                return self.async_abort(reason="already_configured")
            await self.async_set_unique_id(owner.unique_id)
            self._abort_if_unique_id_configured()
        if unsafe:
            return self.async_abort(reason="not_supported")

        await self.async_set_unique_id(recovery_address or discovery_info.address)
        self._abort_if_unique_id_configured()
        if recovery_address is None and not is_amaran_fixture(discovery_info):
            return self.async_abort(reason="not_supported")
        self._discovery = discovery_info
        self._recovery_address = recovery_address
        self.context["title_placeholders"] = {"name": suggested_title(discovery_info)}
        return await self.async_step_confirm()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            self._recovery_address = self._recovery_addresses.get(address)
            await self.async_set_unique_id(self._recovery_address or address)
            self._abort_if_unique_id_configured()
            self._discovery = self._discovered[address]
            return await self.async_step_confirm()

        current = {
            value.casefold()
            for value in self._async_current_ids(include_ignore=False)
            if isinstance(value, str)
        }
        records = await async_get_pending_records(self.hass)
        self._discovered = {}
        self._recovery_addresses = {}
        for info in bluetooth.async_discovered_service_info(self.hass, True):
            owner, recovery_address, unsafe = self._proxy_identity_context(
                info, records
            )
            if owner is not None or unsafe:
                continue
            unique_id = recovery_address or info.address
            if unique_id.casefold() in current:
                continue
            if recovery_address is None and not is_amaran_fixture(info):
                continue
            self._discovered[info.address] = info
            if recovery_address is not None:
                self._recovery_addresses[info.address] = recovery_address
        if not self._discovered:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): vol.In(
                        {
                            address: suggested_title(info)
                            for address, info in self._discovered.items()
                        }
                    )
                }
            ),
        )

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._discovery is not None
        info = self._discovery

        if user_input is None:
            return self._confirm_form(info, {}, {})

        if errors := capability_errors(user_input):
            return self._confirm_form(info, errors, user_input)

        try:
            data = await self._async_provision(info)
        except (OSError, PendingProvisionError) as err:
            _LOGGER.error(
                "could not safely save provisioning credentials for %s: %s",
                info.address,
                err,
            )
            return self._confirm_form(info, {"base": "provisioning_failed"}, user_input)
        except ProvisioningError as err:
            _LOGGER.error("provisioning %s failed: %s", info.address, err)
            return self._confirm_form(
                info, {"base": self._failure_reason(info)}, user_input
            )
        except (BleakError, TimeoutError) as err:
            _LOGGER.error("could not reach %s: %s", info.address, err)
            return self._confirm_form(info, {"base": "cannot_connect"}, user_input)

        return self.async_create_entry(
            title=suggested_title(info),
            data=data,
            options=options_for_profile(user_input),
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Re-provision a factory-reset fixture without replacing HA identity."""
        entry = self._get_reconfigure_entry()
        self.context["title_placeholders"] = {"name": entry.title}

        if user_input is not None and (address := user_input.get(CONF_ADDRESS)):
            if (info := self._discovered.get(address)) is None:
                return self._reconfigure_form(entry, {"base": "no_devices_found"})
            try:
                provisioned = await async_reprovision_fixture(
                    self.hass,
                    info,
                    recovery_address=entry.data[CONF_ADDRESS],
                    prepared_data=entry.data,
                )
            except (OSError, PendingProvisionError) as err:
                _LOGGER.error(
                    "could not safely save replacement credentials for %s: %s",
                    info.address,
                    err,
                )
                return self._reconfigure_form(entry, {"base": "provisioning_failed"})
            except ProvisioningError as err:
                _LOGGER.error("re-provisioning %s failed: %s", info.address, err)
                return self._reconfigure_form(entry, {"base": "provisioning_failed"})
            except (BleakError, TimeoutError) as err:
                _LOGGER.error("could not reach reset fixture %s: %s", info.address, err)
                return self._reconfigure_form(entry, {"base": "cannot_connect"})

            async_update_reprovisioned_entry(self.hass, entry, provisioned)
            return self.async_abort(reason="reconfigure_successful")

        return self._reconfigure_form(entry, {})

    def _reconfigure_form(
        self, entry: ConfigEntry, errors: dict[str, str]
    ) -> ConfigFlowResult:
        """Offer current reset candidates and let an empty form rescan."""
        self._discovered = async_reprovision_candidates(
            self.hass, entry, is_amaran_fixture
        )
        if not self._discovered:
            return self.async_show_form(
                step_id="reconfigure",
                data_schema=vol.Schema({}),
                errors=errors or {"base": "no_devices_found"},
                description_placeholders={"name": entry.title},
            )

        hints = (
            entry.data.get(CONF_TRANSPORT_ADDRESS),
            entry.data.get(CONF_ADDRESS),
        )
        preferred = next(
            (
                address
                for hint in hints
                if isinstance(hint, str)
                for address in self._discovered
                if address.casefold() == hint.casefold()
            ),
            next(iter(self._discovered)),
        )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS, default=preferred): vol.In(
                        {
                            address: suggested_title(info)
                            for address, info in self._discovered.items()
                        }
                    )
                }
            ),
            errors=errors,
            description_placeholders={"name": entry.title},
        )

    def _failure_reason(self, info: BluetoothServiceInfoBleak) -> str:
        """Pick the most useful message after provisioning has already failed.

        Belonging to another mesh is the common cause, but it cannot be tested
        up front: advertisement caches are additive, so BlueZ and habluetooth
        both keep serving the proxy service data a fixture published before it
        was factory reset. Treating that as proof would refuse fixtures that
        are in fact ready to adopt, so the check only ever refines an error.
        """
        latest = (
            bluetooth.async_last_service_info(self.hass, info.address, True) or info
        )
        if MESH_PROXY_SERVICE in latest.service_data:
            return "already_provisioned"
        return "provisioning_failed"

    def _confirm_form(
        self,
        info: BluetoothServiceInfoBleak,
        errors: dict[str, str],
        values: dict[str, Any],
    ) -> ConfigFlowResult:
        """Ask for the capabilities that cannot be read over the mesh.

        Fixture capabilities are indistinguishable on the wire: lights accept
        and echo commands for output modes their LEDs may not actually render.
        Asking keeps unsupported controls out of Home Assistant.
        """
        return self.async_show_form(
            step_id="confirm",
            errors=errors,
            description_placeholders={
                "name": suggested_title(info),
                "address": info.address,
            },
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_MODEL,
                        default=values.get(CONF_MODEL, DEFAULT_PROFILE),
                    ): _model_selector(),
                    vol.Required(
                        CONF_SUPPORTS_CCT,
                        default=values.get(CONF_SUPPORTS_CCT, DEFAULT_SUPPORTS_CCT),
                    ): selector.BooleanSelector(),
                    vol.Required(
                        CONF_SUPPORTS_COLOR,
                        default=values.get(CONF_SUPPORTS_COLOR, DEFAULT_SUPPORTS_COLOR),
                    ): selector.BooleanSelector(),
                    vol.Required(
                        CONF_SUPPORTS_GM,
                        default=values.get(CONF_SUPPORTS_GM, DEFAULT_SUPPORTS_GM),
                    ): selector.BooleanSelector(),
                    vol.Required(
                        CONF_MIN_KELVIN,
                        default=values.get(CONF_MIN_KELVIN, DEFAULT_MIN_KELVIN),
                    ): _kelvin_selector(),
                    vol.Required(
                        CONF_MAX_KELVIN,
                        default=values.get(CONF_MAX_KELVIN, DEFAULT_MAX_KELVIN),
                    ): _kelvin_selector(),
                }
            ),
        )

    async def _async_provision(self, info: BluetoothServiceInfoBleak) -> dict[str, Any]:
        """Delegate provisioning for config-flow and non-flow callers alike."""
        if self._recovery_address is not None:
            return await async_provision_fixture(
                self.hass,
                info,
                _recovery_address=self._recovery_address,
                _require_proxy_identity=True,
            )
        return await async_provision_fixture(self.hass, info)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return AmaranOptionsFlow()


class AmaranOptionsFlow(OptionsFlow):
    """Fixture capabilities that cannot be read over the mesh."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            if errors := capability_errors(user_input):
                return self._show_form(user_input, errors)
            return self.async_create_entry(data=options_for_profile(user_input))
        return self._show_form(_options_form_values(self.config_entry), {})

    def _show_form(
        self, values: dict[str, Any], errors: dict[str, str]
    ) -> ConfigFlowResult:
        return self.async_show_form(
            step_id="init",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_MODEL,
                        default=values.get(CONF_MODEL, DEFAULT_PROFILE),
                    ): _model_selector(),
                    vol.Required(
                        CONF_SUPPORTS_CCT,
                        default=values.get(CONF_SUPPORTS_CCT, DEFAULT_SUPPORTS_CCT),
                    ): selector.BooleanSelector(),
                    vol.Required(
                        CONF_SUPPORTS_COLOR,
                        default=values.get(CONF_SUPPORTS_COLOR, DEFAULT_SUPPORTS_COLOR),
                    ): selector.BooleanSelector(),
                    vol.Required(
                        CONF_SUPPORTS_GM,
                        default=values.get(CONF_SUPPORTS_GM, DEFAULT_SUPPORTS_GM),
                    ): selector.BooleanSelector(),
                    vol.Required(
                        CONF_MIN_KELVIN,
                        default=values.get(CONF_MIN_KELVIN, DEFAULT_MIN_KELVIN),
                    ): _kelvin_selector(),
                    vol.Required(
                        CONF_MAX_KELVIN,
                        default=values.get(CONF_MAX_KELVIN, DEFAULT_MAX_KELVIN),
                    ): _kelvin_selector(),
                }
            ),
        )
