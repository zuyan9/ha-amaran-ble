"""Repairs for fixtures that lost their Bluetooth Mesh membership."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from bleak.exc import BleakError
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .amaranble.provisioning import ProvisioningError
from .const import CONF_TRANSPORT_ADDRESS, DOMAIN
from .pending import PendingProvisionError
from .reconfiguration import (
    async_reprovision_candidates,
    async_update_reprovisioned_entry,
)

_LOGGER = logging.getLogger(__name__)

_ISSUE_PREFIX = "factory_reset"


def _issue_id(entry_id: str) -> str:
    """Return the stable issue ID for one config entry."""
    return f"{_ISSUE_PREFIX}_{entry_id}"


@callback
def async_create_factory_reset_issue(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Report that a configured fixture has left Home Assistant's mesh."""
    ir.async_create_issue(
        hass=hass,
        domain=DOMAIN,
        issue_id=_issue_id(entry.entry_id),
        data={"entry_id": entry.entry_id},
        is_fixable=True,
        is_persistent=True,
        severity=ir.IssueSeverity.ERROR,
        translation_key="factory_reset",
        translation_placeholders={"name": entry.title},
    )


@callback
def async_delete_factory_reset_issue(hass: HomeAssistant, entry_id: str) -> None:
    """Clear a resolved or orphaned factory-reset issue."""
    ir.async_delete_issue(
        hass=hass,
        domain=DOMAIN,
        issue_id=_issue_id(entry_id),
    )


async def _async_reprovision_candidates(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, BluetoothServiceInfoBleak]:
    """Return reset fixtures and authenticated interrupted-repair proxies."""
    # Avoid loading the config-flow module just to register lifecycle issue
    # callbacks during normal integration setup.
    from .config_flow import _replacement_model_matches_entry, is_amaran_fixture

    candidates = await async_reprovision_candidates(hass, entry, is_amaran_fixture)
    return {
        address: info
        for address, info in candidates.items()
        if _replacement_model_matches_entry(entry, info)
    }


def _preferred_address(
    entry: ConfigEntry, candidates: dict[str, BluetoothServiceInfoBleak]
) -> str:
    """Prefer the most recently working route without requiring a fixed MAC."""
    hints = (
        entry.data.get(CONF_TRANSPORT_ADDRESS),
        entry.data.get(CONF_ADDRESS),
    )
    for hint in hints:
        if not isinstance(hint, str):
            continue
        if match := next(
            (
                address
                for address in candidates
                if address.casefold() == hint.casefold()
            ),
            None,
        ):
            return match
    return next(iter(candidates))


def _candidate_title(info: BluetoothServiceInfoBleak) -> str:
    """Return a fixture label that remains distinct for stock BLE names."""
    from .config_flow import suggested_title

    return suggested_title(info)


class FactoryResetRepairFlow(RepairsFlow):
    """Re-provision a reset fixture into its existing Home Assistant entry."""

    def __init__(self, entry_id: str) -> None:
        """Initialize a repair that remains safe if the entry is removed."""
        self._entry_id = entry_id
        self._discovered: dict[str, BluetoothServiceInfoBleak] = {}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        """Find the reset fixture and perform the requested re-provisioning."""
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            async_delete_factory_reset_issue(self.hass, self._entry_id)
            return self.async_abort(reason="entry_removed")

        # An empty submission is useful when the first scan found no fixture:
        # it refreshes the discovery list without dismissing the issue.
        if user_input is not None and (address := user_input.get(CONF_ADDRESS)):
            info = self._discovered.get(address)
            if info is None:
                return await self._async_show_form(entry, {"base": "no_devices_found"})

            try:
                # Import lazily so the config flow can finish registering even
                # when Home Assistant loads this Repairs platform in parallel.
                from .config_flow import async_reprovision_fixture

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
                return await self._async_show_form(
                    entry, {"base": "provisioning_failed"}
                )
            except ProvisioningError as err:
                _LOGGER.error("re-provisioning %s failed: %s", info.address, err)
                return await self._async_show_form(
                    entry, {"base": "provisioning_failed"}
                )
            except (BleakError, TimeoutError) as err:
                _LOGGER.error("could not reach reset fixture %s: %s", info.address, err)
                return await self._async_show_form(entry, {"base": "cannot_connect"})

            # Replace every mesh credential in one config-entry update while
            # retaining the entry, entity IDs, and device-registry identifier.
            async_update_reprovisioned_entry(self.hass, entry, provisioned)
            return self.async_create_entry(data={})

        return await self._async_show_form(entry, {})

    async def _async_show_form(
        self, entry: ConfigEntry, errors: dict[str, str]
    ) -> RepairsFlowResult:
        """Show a retryable selector without trusting cached bearer state."""
        self._discovered = await _async_reprovision_candidates(self.hass, entry)
        if not self._discovered:
            return self.async_show_form(
                step_id="init",
                data_schema=vol.Schema({}),
                errors=errors or {"base": "no_devices_found"},
                description_placeholders={"name": entry.title},
            )

        preferred = _preferred_address(entry, self._discovered)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS, default=preferred): vol.In(
                        {
                            address: _candidate_title(info)
                            for address, info in self._discovered.items()
                        }
                    )
                }
            ),
            errors=errors,
            description_placeholders={"name": entry.title},
        )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create a real recovery flow for a factory-reset issue."""
    if not issue_id.startswith(f"{_ISSUE_PREFIX}_") or data is None:
        raise ValueError(f"unknown amaran BLE repair issue: {issue_id}")
    entry_id = data.get("entry_id")
    if not isinstance(entry_id, str):
        raise ValueError(f"repair issue {issue_id} has no config entry")
    if issue_id != _issue_id(entry_id):
        raise ValueError(f"repair issue {issue_id} does not match its config entry")
    return FactoryResetRepairFlow(entry_id)
