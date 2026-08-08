"""Config flow for amaran BLE.

Adding a fixture provisions it into a private Bluetooth Mesh network that this
integration creates and owns. The keys never leave Home Assistant, and nothing
has to be extracted from amaran's own app.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

import voluptuous as vol
from bleak import BleakClient
from bleak.exc import BleakError
from bleak_retry_connector import establish_connection
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import selector

from .amaranble import crypto
from .amaranble.gatt import MESH_PROVISIONING_SERVICE, MESH_PROXY_SERVICE
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
    CONF_NEEDS_CONFIGURATION,
    CONF_NET_KEY,
    CONF_NUM_ELEMENTS,
    CONF_SUPPORTS_CCT,
    CONF_SUPPORTS_COLOR,
    CONF_SUPPORTS_GM,
    CONF_UNICAST_ADDRESS,
    DEFAULT_MAX_KELVIN,
    DEFAULT_MIN_KELVIN,
    DEFAULT_SUPPORTS_CCT,
    DEFAULT_SUPPORTS_COLOR,
    DEFAULT_SUPPORTS_GM,
    DOMAIN,
    NODE_ADDRESS,
    PROVISIONER_ADDRESS,
    TELINK_ADDRESS_PREFIX,
)
from .pending import (
    PendingProvisionError,
    async_get_pending,
    async_remove_pending,
    async_save_pending,
)

_LOGGER = logging.getLogger(__name__)


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


class AmaranConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle discovery, provisioning and re-provisioning."""

    VERSION = 1
    MINOR_VERSION = 2

    def __init__(self) -> None:
        self._discovery: BluetoothServiceInfoBleak | None = None
        self._discovered: dict[str, BluetoothServiceInfoBleak] = {}

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        if not is_amaran_fixture(discovery_info):
            return self.async_abort(reason="not_supported")
        self._discovery = discovery_info
        self.context["title_placeholders"] = {"name": suggested_title(discovery_info)}
        return await self.async_step_confirm()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address)
            self._abort_if_unique_id_configured()
            self._discovery = self._discovered[address]
            return await self.async_step_confirm()

        current = self._async_current_ids()
        self._discovered = {
            info.address: info
            for info in bluetooth.async_discovered_service_info(self.hass, True)
            if is_amaran_fixture(info) and info.address not in current
        }
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

        supports_cct = bool(user_input[CONF_SUPPORTS_CCT])
        if user_input[CONF_SUPPORTS_GM] and not supports_cct:
            return self._confirm_form(
                info, {CONF_SUPPORTS_GM: "gm_requires_cct"}, user_input
            )
        if user_input[CONF_SUPPORTS_COLOR] and not supports_cct:
            return self._confirm_form(
                info, {CONF_SUPPORTS_COLOR: "color_requires_cct"}, user_input
            )
        if supports_cct and user_input[CONF_MIN_KELVIN] >= user_input[CONF_MAX_KELVIN]:
            return self._confirm_form(
                info, {CONF_MAX_KELVIN: "invalid_range"}, user_input
            )

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
            options={
                CONF_SUPPORTS_CCT: supports_cct,
                CONF_SUPPORTS_COLOR: user_input[CONF_SUPPORTS_COLOR],
                CONF_SUPPORTS_GM: supports_cct and bool(user_input[CONF_SUPPORTS_GM]),
                CONF_MIN_KELVIN: int(user_input[CONF_MIN_KELVIN]),
                CONF_MAX_KELVIN: int(user_input[CONF_MAX_KELVIN]),
            },
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
        """Provision the fixture or recover credentials from an interrupted flow."""
        pending = await async_get_pending(self.hass, info.address)
        if pending is not None and "data" in pending:
            # New records distinguish credentials prepared before the
            # irreversible Provisioning Data PDU from a session for which the
            # fixture sent Complete. If it plainly still advertises only the
            # provisioning service, the PDU never committed and those keys are
            # safe to discard. Ambiguous/cached advertisements deliberately
            # favor retaining keys that may now be the only way into the node.
            if (
                not pending.get("committed", False)
                and MESH_PROVISIONING_SERVICE in info.service_data
                and MESH_PROXY_SERVICE not in info.service_data
            ):
                await async_remove_pending(self.hass, info.address)
                pending = None
            else:
                pending = pending["data"]

        if pending is not None:
            _LOGGER.info(
                "recovering previously provisioned fixture %s from durable credentials",
                info.address,
            )
            return pending

        net_key = crypto.random_bytes(16)
        app_key = crypto.random_bytes(16)
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
                CONF_NEEDS_CONFIGURATION: True,
            }
            await async_save_pending(
                self.hass,
                info.address,
                {"data": data, "committed": False},
            )

        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, info.address, connectable=True
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
            with contextlib.suppress(Exception):
                await client.disconnect()

        if data is None:
            raise ProvisioningError(
                "provisioning completed without saving recovery credentials"
            )
        # Mark the unambiguously successful case. If Home Assistant stopped
        # between Provisioning Data and Complete, recovery can still infer the
        # committed state from the node's 0x1828 advertisement.
        await async_save_pending(
            self.hass,
            info.address,
            {"data": data, "committed": True},
        )
        return data

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
            supports_cct = bool(user_input[CONF_SUPPORTS_CCT])
            if user_input[CONF_SUPPORTS_GM] and not supports_cct:
                return self._show_form(
                    user_input, {CONF_SUPPORTS_GM: "gm_requires_cct"}
                )
            if user_input[CONF_SUPPORTS_COLOR] and not supports_cct:
                return self._show_form(
                    user_input, {CONF_SUPPORTS_COLOR: "color_requires_cct"}
                )
            if (
                supports_cct
                and user_input[CONF_MIN_KELVIN] >= user_input[CONF_MAX_KELVIN]
            ):
                return self._show_form(user_input, {CONF_MAX_KELVIN: "invalid_range"})
            return self.async_create_entry(
                data={
                    CONF_SUPPORTS_CCT: supports_cct,
                    CONF_SUPPORTS_COLOR: bool(user_input[CONF_SUPPORTS_COLOR]),
                    CONF_SUPPORTS_GM: supports_cct
                    and bool(user_input[CONF_SUPPORTS_GM]),
                    CONF_MIN_KELVIN: int(user_input[CONF_MIN_KELVIN]),
                    CONF_MAX_KELVIN: int(user_input[CONF_MAX_KELVIN]),
                }
            )
        return self._show_form(dict(self.config_entry.options), {})

    def _show_form(
        self, values: dict[str, Any], errors: dict[str, str]
    ) -> ConfigFlowResult:
        return self.async_show_form(
            step_id="init",
            errors=errors,
            data_schema=vol.Schema(
                {
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
