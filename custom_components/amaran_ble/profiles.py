"""Fixture profiles for model-specific amaran features."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from .const import (
    CONF_MODEL,
    DEFAULT_MAX_KELVIN,
    DEFAULT_MIN_KELVIN,
    DEFAULT_PROFILE,
    PROFILE_ACE_25X,
    PROFILE_GENERIC,
)

EFFECT_OFF: Final = "off"


@dataclass(frozen=True, slots=True)
class FixtureProfile:
    """Capabilities that are safe to expose for one fixture family."""

    key: str
    name: str
    supports_cct: bool
    supports_color: bool
    supports_gm: bool
    min_kelvin: int
    max_kelvin: int
    effects: tuple[str, ...] = ()
    supports_boost: bool = False
    boost_min_kelvin: int | None = None
    boost_max_kelvin: int | None = None
    supports_fan: bool = False
    fan_modes: tuple[str, ...] = ()
    supports_power: bool = False
    supports_version: bool = False

    @property
    def supports_effects(self) -> bool:
        """Return whether this profile has verified built-in effects."""
        return bool(self.effects)


GENERIC_PROFILE: Final = FixtureProfile(
    key=PROFILE_GENERIC,
    name="Generic amaran light",
    supports_cct=True,
    supports_color=False,
    supports_gm=False,
    min_kelvin=DEFAULT_MIN_KELVIN,
    max_kelvin=DEFAULT_MAX_KELVIN,
)

ACE_25X_PROFILE: Final = FixtureProfile(
    key=PROFILE_ACE_25X,
    name="amaran Ace 25x",
    supports_cct=True,
    supports_color=False,
    supports_gm=False,
    min_kelvin=2700,
    max_kelvin=6500,
    effects=(
        EFFECT_OFF,
        "Fireworks",
        "Faulty Bulb",
        "Lightning",
        "TV",
        "Pulsing",
        "Strobe",
        "Explosion",
        "Fire",
        "Paparazzi",
    ),
    supports_boost=True,
    boost_min_kelvin=3800,
    boost_max_kelvin=5500,
    supports_fan=True,
    fan_modes=("silent", "smart"),
    supports_power=True,
    supports_version=True,
)

PROFILES: Final = {
    PROFILE_GENERIC: GENERIC_PROFILE,
    PROFILE_ACE_25X: ACE_25X_PROFILE,
}


def get_fixture_profile(model: str | None) -> FixtureProfile:
    """Resolve a persisted model safely, falling back to generic controls."""
    return PROFILES.get(model or DEFAULT_PROFILE, GENERIC_PROFILE)


def profile_for_entry(entry: Any) -> FixtureProfile:
    """Resolve the profile selected in a Home Assistant config entry."""
    model = entry.options.get(CONF_MODEL, entry.data.get(CONF_MODEL, DEFAULT_PROFILE))
    return get_fixture_profile(model)
