"""Profile selection and config-entry migration behavior."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("homeassistant")

from custom_components.amaran_ble import async_migrate_entry
from custom_components.amaran_ble.config_flow import options_for_profile
from custom_components.amaran_ble.const import (
    CONF_MAX_KELVIN,
    CONF_MIN_KELVIN,
    CONF_MODEL,
    CONF_SUPPORTS_CCT,
    CONF_SUPPORTS_COLOR,
    CONF_SUPPORTS_GM,
    PROFILE_ACE_25X,
    PROFILE_GENERIC,
)
from custom_components.amaran_ble.profiles import ACE_25X_PROFILE, profile_for_entry


def test_ace_profile_overrides_manual_capabilities() -> None:
    """Selecting Ace always uses its verified fixed hardware profile."""
    options = options_for_profile(
        {
            CONF_MODEL: PROFILE_ACE_25X,
            CONF_SUPPORTS_CCT: False,
            CONF_SUPPORTS_COLOR: True,
            CONF_SUPPORTS_GM: True,
            CONF_MIN_KELVIN: 800,
            CONF_MAX_KELVIN: 20000,
        }
    )

    assert options == {
        CONF_MODEL: PROFILE_ACE_25X,
        CONF_SUPPORTS_CCT: True,
        CONF_SUPPORTS_COLOR: False,
        CONF_SUPPORTS_GM: False,
        CONF_MIN_KELVIN: 2700,
        CONF_MAX_KELVIN: 6500,
    }
    assert ACE_25X_PROFILE.boost_min_kelvin == 3800
    assert ACE_25X_PROFILE.boost_max_kelvin == 5500


def test_missing_or_unknown_profile_resolves_to_generic() -> None:
    """Unsafe model-specific commands are never inferred for legacy entries."""
    missing = SimpleNamespace(options={}, data={})
    unknown = SimpleNamespace(options={CONF_MODEL: "future_model"}, data={})

    assert profile_for_entry(missing).key == PROFILE_GENERIC
    assert profile_for_entry(unknown).key == PROFILE_GENERIC


@pytest.mark.asyncio
async def test_existing_entry_stays_rollback_compatible_and_resolves_generic() -> None:
    """The additive profile option does not force a config-entry version bump."""
    original = {
        CONF_SUPPORTS_CCT: True,
        CONF_SUPPORTS_COLOR: True,
        CONF_SUPPORTS_GM: True,
        CONF_MIN_KELVIN: 2500,
        CONF_MAX_KELVIN: 10000,
    }
    entry = SimpleNamespace(version=1, minor_version=2, options=original)
    hass = SimpleNamespace(config_entries=SimpleNamespace(async_update_entry=Mock()))

    assert await async_migrate_entry(hass, entry)
    hass.config_entries.async_update_entry.assert_not_called()
    assert profile_for_entry(SimpleNamespace(options=original, data={})).key == (
        PROFILE_GENERIC
    )


@pytest.mark.asyncio
async def test_pre_release_minor_three_is_normalized_for_rollback() -> None:
    """A transient pre-release entry version remains loadable by release 0.3.1."""
    entry = SimpleNamespace(version=1, minor_version=3, options={})
    hass = SimpleNamespace(config_entries=SimpleNamespace(async_update_entry=Mock()))

    assert await async_migrate_entry(hass, entry)
    hass.config_entries.async_update_entry.assert_called_once_with(
        entry, minor_version=2
    )


@pytest.mark.asyncio
async def test_prototype_default_cct_range_is_narrowly_migrated() -> None:
    """Only the prototype's exact bi-colour defaults become the Ace range."""
    options = {
        CONF_SUPPORTS_COLOR: False,
        CONF_MIN_KELVIN: 2500,
        CONF_MAX_KELVIN: 7500,
    }
    entry = SimpleNamespace(version=1, minor_version=1, options=options)
    update = Mock()
    hass = SimpleNamespace(config_entries=SimpleNamespace(async_update_entry=update))

    assert await async_migrate_entry(hass, entry)
    update.assert_called_once_with(
        entry,
        version=1,
        minor_version=2,
        options={
            CONF_SUPPORTS_COLOR: False,
            CONF_MIN_KELVIN: 2700,
            CONF_MAX_KELVIN: 6500,
        },
    )


@pytest.mark.asyncio
async def test_prototype_custom_cct_range_is_preserved() -> None:
    """Migration never overwrites a range the user intentionally configured."""
    options = {
        CONF_SUPPORTS_COLOR: False,
        CONF_MIN_KELVIN: 2000,
        CONF_MAX_KELVIN: 10000,
    }
    entry = SimpleNamespace(version=1, minor_version=1, options=options)
    update = Mock()
    hass = SimpleNamespace(config_entries=SimpleNamespace(async_update_entry=update))

    assert await async_migrate_entry(hass, entry)
    update.assert_called_once_with(
        entry,
        version=1,
        minor_version=2,
        options=options,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("version", "minor_version"), [(1, 4), (2, 0)])
async def test_future_entry_versions_are_rejected_without_mutation(
    version: int, minor_version: int
) -> None:
    """Older integration code must not reinterpret an unknown future schema."""
    entry = SimpleNamespace(
        version=version,
        minor_version=minor_version,
        options={},
    )
    update = Mock()
    hass = SimpleNamespace(config_entries=SimpleNamespace(async_update_entry=update))

    assert not await async_migrate_entry(hass, entry)
    update.assert_not_called()
