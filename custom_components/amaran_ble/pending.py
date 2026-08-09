"""Durable recovery records for an in-progress Mesh provisioning commit."""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_STORE_KEY = f"{DOMAIN}.pending"
_LOCK_KEY = f"{DOMAIN}.pending_lock"


class PendingProvisionError(RuntimeError):
    """A provisioning recovery record could not be persisted."""


def _store(hass: HomeAssistant) -> Store[dict]:
    return Store(
        hass,
        1,
        _STORE_KEY,
        private=True,
        atomic_writes=True,
    )


def _lock(hass: HomeAssistant) -> asyncio.Lock:
    lock = hass.data.get(_LOCK_KEY)
    if lock is None:
        lock = hass.data[_LOCK_KEY] = asyncio.Lock()
    return lock


async def async_get_pending(hass: HomeAssistant, address: str) -> dict[str, Any] | None:
    """Return the recovery record for one Bluetooth address, if any."""
    stored = await _store(hass).async_load() or {}
    record = stored.get("fixtures", {}).get(address.upper())
    return dict(record) if record is not None else None


async def async_get_pending_records(
    hass: HomeAssistant,
) -> dict[str, dict[str, Any]]:
    """Return every structurally readable recovery record by stable address.

    Provisioned Mesh nodes may resume advertising through a different Bluetooth
    address after the irreversible Provisioning Data PDU.  Callers can use this
    snapshot to identify an otherwise orphaned record cryptographically; the
    records remain keyed by their original address so that key is still the
    stable config-entry and entity identity.
    """
    stored = await _store(hass).async_load() or {}
    if not isinstance(stored, dict):
        return {}
    fixtures = stored.get("fixtures", {})
    if not isinstance(fixtures, dict):
        return {}
    return {
        address: dict(record)
        for address, record in fixtures.items()
        if isinstance(address, str) and address and isinstance(record, dict)
    }


async def async_save_pending(
    hass: HomeAssistant, address: str, record: dict[str, Any]
) -> None:
    """Atomically save and verify credentials before provisioning commits."""
    async with _lock(hass):
        store = _store(hass)
        stored = await store.async_load() or {}
        fixtures = dict(stored.get("fixtures", {}))
        fixtures[address.upper()] = record
        expected = {"fixtures": fixtures}
        await store.async_save(expected)

        if await _store(hass).async_load() != expected:
            raise PendingProvisionError(
                "provisioning credentials could not be persisted"
            )


async def async_remove_pending(hass: HomeAssistant, address: str) -> None:
    """Forget a recovery record once its config entry is being removed."""
    async with _lock(hass):
        store = _store(hass)
        stored = await store.async_load() or {}
        fixtures = dict(stored.get("fixtures", {}))
        if fixtures.pop(address.upper(), None) is None:
            return
        if not fixtures:
            await store.async_remove()
            return
        expected = {"fixtures": fixtures}
        await store.async_save(expected)
        if await _store(hass).async_load() != expected:
            raise PendingProvisionError("pending provisioning record was not updated")
