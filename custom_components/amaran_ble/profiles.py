"""Static fixture catalog derived from Sidus Link's bundled model data.

The app's ``fixtureConfig.json`` describes steady-light capabilities and
protocol-family gates for each product UUID. ``catalog_capabilities`` preserves
the full evidence set, while derived runtime fields expose only command paths
with safe defaults and Home Assistant behavior.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Final, Literal

from .const import (
    CONF_MODEL,
    DEFAULT_MAX_KELVIN,
    DEFAULT_MIN_KELVIN,
    DEFAULT_PROFILE,
    PROFILE_ACE_25X,
    PROFILE_GENERIC,
)

EFFECT_OFF: Final = "off"

# Keep this order stable: it is the order users see in Home Assistant.  Only
# unversioned ``systemfx_* == 1`` flags from the bundled app catalog are used.
_FX_DAYLIGHT: Final = (
    EFFECT_OFF,
    "Fireworks",
    "Faulty Bulb",
    "Lightning",
    "TV",
    "Pulsing",
    "Strobe",
    "Explosion",
    "Paparazzi",
)
_FX_BICOLOR: Final = (
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
)
_FX_VERGE: Final = (
    EFFECT_OFF,
    "Fireworks",
    "Faulty Bulb",
    "Lightning",
    "TV",
    "Strobe",
    "Explosion",
    "Fire",
)
_FX_COLOR_CLASSIC: Final = (
    EFFECT_OFF,
    "Fireworks",
    "Faulty Bulb",
    "Lightning",
    "TV",
    "Pulsing",
    "Fire",
    "Paparazzi",
    "Cop Car",
    "Party Lights",
)
_FX_COLOR_WELDING: Final = (
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
    "Welding",
    "Cop Car",
    "Party Lights",
)
_FX_BICOLOR_COP: Final = (*_FX_BICOLOR, "Cop Car")
_FX_BICOLOR_CANDLE: Final = (*_FX_BICOLOR, "Candle")
_FX_NOVA_II: Final = (EFFECT_OFF, "Club Lights", "Candle", "Color Chase")
_FX_FULL: Final = (
    *_FX_BICOLOR,
    "Club Lights",
    "Candle",
    "Welding",
    "Cop Car",
    "Color Chase",
    "Party Lights",
)

# Command 33 has a complete, defaults-proven codec for these seven names.  The
# catalog also uses its pixel-effect field for a few unrelated SM5c modes; do
# not route those through the command-33 implementation merely because they
# share the metadata key.
_PIXEL_EFFECT_NAMES: Final = frozenset(
    {
        "Color Fade",
        "Color Cycle",
        "One Pixel Chase",
        "Two Pixel Chase",
        "Three Pixel Chase",
        "Pixel Fire",
        "Rainbow",
    }
)

# A fan write is still gated by the capabilities in the fixture's live fan
# report.  Catalog profiles merely allow every mode the report format can name.
ALL_FAN_MODES: Final = (
    "manual",
    "smart",
    "max",
    "off",
    "high",
    "medium",
    "low",
    "silent",
)


@dataclass(frozen=True, slots=True)
class CatalogSystemEffect:
    """One command-34 SystemFX2 effect advertised by the app catalog."""

    name: str
    generation: Literal[2, 3]


@dataclass(frozen=True, slots=True)
class CatalogSteadyColorCapabilities:
    """HSI cmd1, Gel cmd3, RGB/RGBW cmd4, XY cmd5 and G/M catalog gates."""

    hsi: bool = False
    rgb: bool = False
    xy: bool = False
    gel: bool = False
    advanced_hsi: bool = False
    advanced_hsi_version: str | None = None
    gm: bool = False
    gm_min: int = 0
    gm_max: int = 0
    gm_v2_version: str | None = None


@dataclass(frozen=True, slots=True)
class CatalogCctExtensionCapabilities:
    """Extended-CCT gate and range from the app catalog."""

    supported: bool = False
    min_kelvin: int = 0
    max_kelvin: int = 0


@dataclass(frozen=True, slots=True)
class CatalogPixelEffectCapabilities:
    """Command-33 pixel-effect names and the app's raw ``pixel_num`` value."""

    pixel_num: int = 0
    effects: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CatalogPartitionCapabilities:
    """Partition gate, version and geometry fields from the app catalog."""

    supported: bool = False
    version: str | None = None
    pixel_x1: int = 0
    pixel_y1: int = 0
    pixel_x2: int = 0
    pixel_y2: int = 0
    pixel_xy: tuple[tuple[int, int], ...] = ()
    v2_supported: bool = False
    v2_location_supported: bool = False


@dataclass(frozen=True, slots=True)
class CatalogMultiPartitionCapabilities:
    """Multi-partition gate and app protocol version."""

    supported: bool = False
    version: str | None = None


@dataclass(frozen=True, slots=True)
class CatalogMagicPixelCapabilities:
    """Magic PixelFX gates and sizing fields from the app catalog."""

    supported: bool = False
    version: str | None = None
    pixel: int = 0
    ppf: int = 0
    rainbow: bool = False
    move: bool = False
    advanced_move: bool = False
    advanced_move_version: str | None = None
    overall: bool = False
    fire: bool = False
    word: bool = False


@dataclass(frozen=True, slots=True)
class CatalogMotionCapabilities:
    """Motion-control gate and app protocol version."""

    supported: bool = False
    version: str | None = None


@dataclass(frozen=True, slots=True)
class CatalogHighSpeedPhotographyCapabilities:
    """High-speed-photography gate, version and intensity bounds."""

    supported: bool = False
    version: str | None = None
    intensity_min: int = 0
    intensity_max: int = 0


_EMPTY_STEADY_COLOR: Final = CatalogSteadyColorCapabilities()
_EMPTY_CCT_EXTENSION: Final = CatalogCctExtensionCapabilities()
_EMPTY_PIXEL_EFFECTS: Final = CatalogPixelEffectCapabilities()
_EMPTY_PARTITION: Final = CatalogPartitionCapabilities()
_EMPTY_MULTI_PARTITION: Final = CatalogMultiPartitionCapabilities()
_EMPTY_MAGIC_PIXEL: Final = CatalogMagicPixelCapabilities()
_EMPTY_MOTION: Final = CatalogMotionCapabilities()
_EMPTY_HIGH_SPEED: Final = CatalogHighSpeedPhotographyCapabilities()


@dataclass(frozen=True, slots=True)
class FixtureCatalogCapabilities:
    """Descriptive app metadata that does not assert runtime implementation."""

    steady_color: CatalogSteadyColorCapabilities = _EMPTY_STEADY_COLOR
    system_fx2: tuple[CatalogSystemEffect, ...] = ()
    pixel_fx: CatalogPixelEffectCapabilities = _EMPTY_PIXEL_EFFECTS
    partition: CatalogPartitionCapabilities = _EMPTY_PARTITION
    multi_partition: CatalogMultiPartitionCapabilities = _EMPTY_MULTI_PARTITION
    magic_pixel: CatalogMagicPixelCapabilities = _EMPTY_MAGIC_PIXEL
    motion: CatalogMotionCapabilities = _EMPTY_MOTION
    high_speed_photography: CatalogHighSpeedPhotographyCapabilities = _EMPTY_HIGH_SPEED
    cct_extension: CatalogCctExtensionCapabilities = _EMPTY_CCT_EXTENSION
    thousand_level_dimming: bool = False
    manual_fx: bool = False
    program_fx: bool = False
    picker_fx: bool = False
    touchbar_fx: bool = False
    music_fx: bool = False


EMPTY_CATALOG_CAPABILITIES: Final = FixtureCatalogCapabilities()


@dataclass(frozen=True, slots=True)
class FixtureFamily:
    """Steady-light capabilities shared by one app catalog family."""

    key: str
    supports_cct: bool
    supports_color: bool
    supports_gm: bool
    min_kelvin: int
    max_kelvin: int


@dataclass(frozen=True, slots=True)
class FixtureProfile:
    """Runtime capabilities and descriptive catalog metadata for one model."""

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
    family: str = "generic"
    manufacturer: str = ""
    app_product_ids: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    hardware_tested: bool = False
    catalog_capabilities: FixtureCatalogCapabilities = EMPTY_CATALOG_CAPABILITIES

    @property
    def supports_effects(self) -> bool:
        """Return whether this profile has cataloged built-in effects."""
        return bool(self.all_effects)

    @property
    def system_effects2(self) -> tuple[str, ...]:
        """Return command-34 effects with app-proven generation-II defaults."""
        return tuple(
            effect.name
            for effect in self.catalog_capabilities.system_fx2
            if effect.generation == 2
        )

    @property
    def pixel_effects(self) -> tuple[str, ...]:
        """Return command-33 effects whose app defaults are fully proven."""
        return tuple(
            effect
            for effect in self.catalog_capabilities.pixel_fx.effects
            if effect in _PIXEL_EFFECT_NAMES
        )

    @property
    def all_effects(self) -> tuple[str, ...]:
        """Return every built-in effect safe to select from Home Assistant."""
        effects = list(self.effects)
        if (self.system_effects2 or self.pixel_effects) and EFFECT_OFF not in effects:
            effects.insert(0, EFFECT_OFF)
        for effect in (*self.system_effects2, *self.pixel_effects):
            if effect not in effects:
                effects.append(effect)
        return tuple(effects)


def _family(
    key: str,
    *,
    minimum: int,
    maximum: int,
    cct: bool = True,
    color: bool = False,
    gm: bool = False,
) -> FixtureFamily:
    return FixtureFamily(key, cct, color, gm, minimum, maximum)


FIXTURE_FAMILIES: Final = {
    "fixed_5600": _family("fixed_5600", minimum=5600, maximum=5600, cct=False),
    "cct_2500_7500": _family("cct_2500_7500", minimum=2500, maximum=7500),
    "cct_2700_6500": _family("cct_2700_6500", minimum=2700, maximum=6500),
    "cct_3200_6500": _family("cct_3200_6500", minimum=3200, maximum=6500),
    "cct_gm_2500_10000": _family(
        "cct_gm_2500_10000", minimum=2500, maximum=10000, gm=True
    ),
    "cct_gm_2700_6500": _family(
        "cct_gm_2700_6500", minimum=2700, maximum=6500, gm=True
    ),
    "hsi_3200_6500": _family("hsi_3200_6500", minimum=3200, maximum=6500, color=True),
    "hsi_gm_1800_20000": _family(
        "hsi_gm_1800_20000",
        minimum=1800,
        maximum=20000,
        color=True,
        gm=True,
    ),
    "hsi_gm_2000_10000": _family(
        "hsi_gm_2000_10000",
        minimum=2000,
        maximum=10000,
        color=True,
        gm=True,
    ),
    "hsi_gm_2300_10000": _family(
        "hsi_gm_2300_10000",
        minimum=2300,
        maximum=10000,
        color=True,
        gm=True,
    ),
    "hsi_gm_2500_10000": _family(
        "hsi_gm_2500_10000",
        minimum=2500,
        maximum=10000,
        color=True,
        gm=True,
    ),
    "hsi_gm_2500_7500": _family(
        "hsi_gm_2500_7500",
        minimum=2500,
        maximum=7500,
        color=True,
        gm=True,
    ),
    "hsi_gm_2700_10000": _family(
        "hsi_gm_2700_10000",
        minimum=2700,
        maximum=10000,
        color=True,
        gm=True,
    ),
    "hsi_gm_3200_6500": _family(
        "hsi_gm_3200_6500",
        minimum=3200,
        maximum=6500,
        color=True,
        gm=True,
    ),
}


@dataclass(frozen=True, slots=True)
class _ModelSpec:
    key: str
    name: str
    manufacturer: str
    family: str
    app_product_ids: tuple[str, ...]
    effects: tuple[str, ...]
    fan: bool = False
    boost: tuple[int, int] | None = None
    aliases: tuple[str, ...] = ()


# One record per distinct model. An app product UUID can have multiple config
# revisions; the four MC UUIDs intentionally converge on one profile because
# their steady capabilities and first-generation effects are identical.
_MODEL_SPECS: Final = (
    _ModelSpec(
        "100d",
        "amaran 100d",
        "amaran",
        "fixed_5600",
        ("40025",),
        _FX_DAYLIGHT,
        fan=True,
    ),
    _ModelSpec(
        "100d_s",
        "amaran 100d S",
        "amaran",
        "fixed_5600",
        ("400N5",),
        _FX_DAYLIGHT,
        fan=True,
    ),
    _ModelSpec(
        "100x",
        "amaran 100x",
        "amaran",
        "cct_2700_6500",
        ("40035",),
        _FX_BICOLOR,
        fan=True,
    ),
    _ModelSpec(
        "100x_s",
        "amaran 100x S",
        "amaran",
        "cct_2700_6500",
        ("400O5",),
        _FX_BICOLOR,
        fan=True,
    ),
    _ModelSpec(
        "150c",
        "amaran 150c",
        "amaran",
        "hsi_gm_2500_7500",
        ("400J5",),
        _FX_COLOR_CLASSIC,
        fan=True,
    ),
    _ModelSpec(
        "200c",
        "amaran 200c",
        "amaran",
        "hsi_gm_2500_7500",
        ("400R5",),
        _FX_COLOR_WELDING,
        fan=True,
    ),
    _ModelSpec(
        "200d",
        "amaran 200d",
        "amaran",
        "fixed_5600",
        ("40045",),
        _FX_DAYLIGHT,
        fan=True,
    ),
    _ModelSpec(
        "200d_s",
        "amaran 200d S",
        "amaran",
        "fixed_5600",
        ("400P5",),
        _FX_DAYLIGHT,
        fan=True,
    ),
    _ModelSpec(
        "200x",
        "amaran 200x",
        "amaran",
        "cct_2700_6500",
        ("40055",),
        _FX_BICOLOR,
        fan=True,
    ),
    _ModelSpec(
        "200x_s",
        "amaran 200x S",
        "amaran",
        "cct_2700_6500",
        ("400Q5",),
        _FX_BICOLOR,
        fan=True,
    ),
    _ModelSpec(
        "300c",
        "amaran 300c",
        "amaran",
        "hsi_gm_2500_7500",
        ("400K5",),
        _FX_COLOR_CLASSIC,
        fan=True,
    ),
    _ModelSpec(
        "300x",
        "amaran 300x",
        "amaran",
        "cct_2700_6500",
        ("40195",),
        _FX_BICOLOR,
        fan=True,
    ),
    _ModelSpec(
        "60d_s",
        "amaran 60d S",
        "amaran",
        "fixed_5600",
        ("400L5",),
        _FX_DAYLIGHT,
        fan=True,
    ),
    _ModelSpec(
        "60x_s",
        "amaran 60x S",
        "amaran",
        "cct_2700_6500",
        ("400M5",),
        _FX_BICOLOR,
        fan=True,
    ),
    _ModelSpec(
        "ace_25c",
        "amaran Ace 25c",
        "amaran",
        "hsi_gm_2300_10000",
        ("400U5",),
        _FX_COLOR_WELDING,
        fan=True,
        boost=(4000, 5000),
    ),
    _ModelSpec(
        PROFILE_ACE_25X,
        "amaran Ace 25x",
        "amaran",
        "cct_2700_6500",
        ("400T5",),
        _FX_BICOLOR,
        fan=True,
        boost=(3800, 5500),
    ),
    _ModelSpec(
        "cob_60d",
        "amaran COB 60d",
        "amaran",
        "fixed_5600",
        ("40065",),
        _FX_DAYLIGHT,
        fan=True,
        aliases=("amaran 60d",),
    ),
    _ModelSpec(
        "cob_60x",
        "amaran COB 60x",
        "amaran",
        "cct_2700_6500",
        ("40075",),
        _FX_BICOLOR,
        fan=True,
        aliases=("amaran 60x",),
    ),
    _ModelSpec(
        "f21c",
        "amaran F21c",
        "amaran",
        "hsi_gm_2500_7500",
        ("400C5",),
        _FX_COLOR_CLASSIC,
    ),
    _ModelSpec(
        "f21x", "amaran F21x", "amaran", "cct_2500_7500", ("400B5",), _FX_BICOLOR
    ),
    _ModelSpec(
        "f22c",
        "amaran F22c",
        "amaran",
        "hsi_gm_2500_7500",
        ("400E5",),
        _FX_COLOR_CLASSIC,
    ),
    _ModelSpec(
        "f22x", "amaran F22x", "amaran", "cct_2500_7500", ("400D5",), _FX_BICOLOR
    ),
    _ModelSpec("go", "amaran Go", "amaran", "cct_2700_6500", ("400V5",), _FX_BICOLOR),
    _ModelSpec(
        "halo_100x",
        "amaran Halo 100x",
        "amaran",
        "cct_2700_6500",
        ("401C5",),
        _FX_BICOLOR,
        fan=True,
    ),
    _ModelSpec(
        "halo_200x",
        "amaran Halo 200x",
        "amaran",
        "cct_2700_6500",
        ("401D5",),
        _FX_BICOLOR,
        fan=True,
    ),
    _ModelSpec(
        "halo_300x",
        "amaran Halo 300x",
        "amaran",
        "cct_2700_6500",
        ("401E5",),
        _FX_BICOLOR,
        fan=True,
    ),
    _ModelSpec(
        "halo_600x",
        "amaran Halo 600x",
        "amaran",
        "cct_2700_6500",
        ("401F5",),
        _FX_BICOLOR,
        fan=True,
    ),
    _ModelSpec(
        "halo_60x",
        "amaran Halo 60x",
        "amaran",
        "cct_2700_6500",
        ("401B5",),
        _FX_BICOLOR,
        fan=True,
    ),
    _ModelSpec(
        "p40x", "amaran P40x", "amaran", "cct_2700_6500", ("400S5",), _FX_BICOLOR
    ),
    _ModelSpec(
        "p60c",
        "amaran P60c",
        "amaran",
        "hsi_gm_2500_7500",
        ("40015",),
        _FX_COLOR_CLASSIC,
        fan=True,
    ),
    _ModelSpec(
        "p60x",
        "amaran P60x",
        "amaran",
        "cct_3200_6500",
        ("40005",),
        _FX_BICOLOR,
        fan=True,
    ),
    _ModelSpec(
        "pano_120c",
        "amaran Pano 120c",
        "amaran",
        "hsi_gm_2300_10000",
        ("400X5",),
        _FX_COLOR_WELDING,
        fan=True,
    ),
    _ModelSpec(
        "pano_60c",
        "amaran Pano 60c",
        "amaran",
        "hsi_gm_2300_10000",
        ("400W5",),
        _FX_COLOR_WELDING,
        fan=True,
    ),
    _ModelSpec(
        "pt1c",
        "amaran PT1c",
        "amaran",
        "hsi_gm_2700_10000",
        ("400G5",),
        _FX_COLOR_CLASSIC,
    ),
    _ModelSpec(
        "pt2c",
        "amaran PT2c",
        "amaran",
        "hsi_gm_2700_10000",
        ("400H5",),
        _FX_COLOR_CLASSIC,
    ),
    _ModelSpec(
        "pt4c",
        "amaran PT4c",
        "amaran",
        "hsi_gm_2700_10000",
        ("400I5",),
        _FX_COLOR_CLASSIC,
    ),
    _ModelSpec(
        "ray_120c",
        "amaran Ray 120c",
        "amaran",
        "hsi_gm_2300_10000",
        ("40165",),
        _FX_COLOR_WELDING,
        fan=True,
    ),
    _ModelSpec(
        "ray_360c",
        "amaran Ray 360c",
        "amaran",
        "hsi_gm_2300_10000",
        ("40185",),
        _FX_COLOR_WELDING,
        fan=True,
    ),
    _ModelSpec(
        "ray_60c",
        "amaran Ray 60c",
        "amaran",
        "hsi_gm_2300_10000",
        ("40145",),
        _FX_COLOR_WELDING,
        fan=True,
    ),
    _ModelSpec(
        "ray_660c",
        "amaran Ray 660c",
        "amaran",
        "hsi_gm_2300_10000",
        ("401A5",),
        _FX_COLOR_WELDING,
        fan=True,
    ),
    _ModelSpec(
        "sm5c",
        "amaran SM5c",
        "amaran",
        "hsi_gm_3200_6500",
        ("400F5",),
        _FX_COLOR_CLASSIC,
    ),
    _ModelSpec("t2c", "amaran T2c", "amaran", "hsi_gm_2500_7500", ("40085",), _FX_FULL),
    _ModelSpec("t4c", "amaran T4c", "amaran", "hsi_gm_2500_7500", ("40095",), _FX_FULL),
    _ModelSpec(
        "verge", "amaran Verge", "amaran", "cct_2700_6500", ("400Y5",), _FX_VERGE
    ),
    _ModelSpec(
        "verge_max",
        "amaran Verge Max",
        "amaran",
        "cct_2700_6500",
        ("400Z5",),
        _FX_VERGE,
    ),
    _ModelSpec(
        "b7c", "B7c", "Aputure", "hsi_gm_2000_10000", ("04005",), _FX_COLOR_CLASSIC
    ),
    _ModelSpec(
        "electro_storm_cs15",
        "Electro Storm CS15",
        "Aputure",
        "hsi_gm_2000_10000",
        ("000K5",),
        _FX_FULL,
        fan=True,
    ),
    _ModelSpec(
        "electro_storm_xt26",
        "Electro Storm XT26",
        "Aputure",
        "cct_gm_2700_6500",
        ("000J5",),
        _FX_BICOLOR_COP,
        fan=True,
    ),
    _ModelSpec(
        "infinibar_pb12",
        "INFINIBAR PB12",
        "Aputure",
        "hsi_gm_2000_10000",
        ("07008",),
        _FX_FULL,
    ),
    _ModelSpec(
        "infinibar_pb3",
        "INFINIBAR PB3",
        "Aputure",
        "hsi_gm_2000_10000",
        ("07006",),
        _FX_FULL,
    ),
    _ModelSpec(
        "infinibar_pb6",
        "INFINIBAR PB6",
        "Aputure",
        "hsi_gm_2000_10000",
        ("07007",),
        _FX_FULL,
    ),
    _ModelSpec(
        "infinimat_16",
        "INFINIMAT 16",
        "Aputure",
        "hsi_gm_2000_10000",
        ("09015",),
        _FX_FULL,
        fan=True,
    ),
    _ModelSpec(
        "infinimat_4",
        "INFINIMAT 4",
        "Aputure",
        "hsi_gm_2000_10000",
        ("09005",),
        _FX_FULL,
        fan=True,
    ),
    _ModelSpec(
        "ls_1200d_pro",
        "LS 1200d Pro",
        "Aputure",
        "fixed_5600",
        ("000E5",),
        _FX_DAYLIGHT,
        fan=True,
    ),
    _ModelSpec(
        "ls_c300d_ii",
        "LS C300d II",
        "Aputure",
        "fixed_5600",
        ("00005",),
        _FX_DAYLIGHT,
        aliases=("LS 300d II",),
    ),
    _ModelSpec(
        "ls_300x", "LS 300x", "Aputure", "cct_2700_6500", ("00045",), _FX_BICOLOR
    ),
    _ModelSpec(
        "ls_600c_pro",
        "LS 600c Pro",
        "Aputure",
        "hsi_gm_2300_10000",
        ("000G5",),
        _FX_FULL,
        fan=True,
    ),
    _ModelSpec(
        "ls_600c_pro_ii",
        "LS 600c Pro II",
        "Aputure",
        "hsi_gm_2300_10000",
        ("000L5",),
        _FX_FULL,
        fan=True,
    ),
    _ModelSpec(
        "ls_600d",
        "LS 600d",
        "Aputure",
        "fixed_5600",
        ("000C5",),
        _FX_DAYLIGHT,
        fan=True,
    ),
    _ModelSpec(
        "ls_600d_pro",
        "LS 600d Pro",
        "Aputure",
        "fixed_5600",
        ("00055",),
        _FX_DAYLIGHT,
        fan=True,
    ),
    _ModelSpec(
        "ls_600x_pro",
        "LS 600X Pro",
        "Aputure",
        "cct_2700_6500",
        ("000D5",),
        _FX_BICOLOR,
        fan=True,
    ),
    _ModelSpec("ls_60d", "LS 60d", "Aputure", "fixed_5600", ("00075",), _FX_DAYLIGHT),
    _ModelSpec("ls_60x", "LS 60x", "Aputure", "cct_2700_6500", ("00085",), _FX_BICOLOR),
    _ModelSpec(
        "mc",
        "MC",
        "Aputure",
        "hsi_3200_6500",
        ("05005", "05006", "05007", "05008"),
        _FX_COLOR_CLASSIC,
    ),
    _ModelSpec(
        "mc_pro", "MC Pro", "Aputure", "hsi_gm_2000_10000", ("05010",), _FX_FULL
    ),
    _ModelSpec(
        "mt_pro", "MT Pro", "Aputure", "hsi_gm_2000_10000", ("000F5",), _FX_FULL
    ),
    _ModelSpec(
        "nova_9_2x1",
        "NOVA 9° 2\u00d71",
        "Aputure",
        "hsi_gm_1800_20000",
        ("02075",),
        _FX_NOVA_II,
        fan=True,
    ),
    _ModelSpec(
        "nova_ii_1x1",
        "NOVA II 1x1",
        "Aputure",
        "hsi_gm_1800_20000",
        ("02085",),
        _FX_NOVA_II,
        fan=True,
    ),
    _ModelSpec(
        "nova_ii_2x1",
        "NOVA II 2x1",
        "Aputure",
        "hsi_gm_1800_20000",
        ("02065",),
        _FX_NOVA_II,
        fan=True,
    ),
    _ModelSpec(
        "nova_p300c", "Nova P300c", "Aputure", "hsi_gm_2000_10000", ("02005",), _FX_FULL
    ),
    _ModelSpec(
        "nova_p600c",
        "Nova P600C",
        "Aputure",
        "hsi_gm_2000_10000",
        ("02035",),
        _FX_FULL,
        fan=True,
        aliases=("Nova P600c",),
    ),
    _ModelSpec(
        "storm_1000c",
        "STORM 1000c",
        "Aputure",
        "hsi_gm_1800_20000",
        ("000P5",),
        _FX_FULL,
        fan=True,
    ),
    _ModelSpec(
        "storm_1200x",
        "STORM 1200x",
        "Aputure",
        "cct_gm_2500_10000",
        ("000M5",),
        _FX_BICOLOR,
        fan=True,
    ),
    _ModelSpec(
        "storm_400x",
        "STORM 400x",
        "Aputure",
        "cct_gm_2500_10000",
        ("000Q5",),
        _FX_BICOLOR,
        fan=True,
    ),
    _ModelSpec(
        "storm_700x",
        "STORM 700x",
        "Aputure",
        "hsi_gm_2500_10000",
        ("000S5",),
        _FX_BICOLOR_CANDLE,
        fan=True,
    ),
    _ModelSpec(
        "storm_80c",
        "STORM 80c",
        "Aputure",
        "hsi_gm_1800_20000",
        ("000N5",),
        _FX_FULL,
        fan=True,
    ),
    _ModelSpec(
        "storm_xt52",
        "STORM XT52",
        "Aputure",
        "hsi_gm_2500_10000",
        ("000R5",),
        _FX_BICOLOR,
        fan=True,
    ),
)


def _ids(value: str) -> frozenset[str]:
    """Build an immutable app product-id set."""
    return frozenset(value.split())


_ALL_CATALOG_PRODUCT_IDS: Final = frozenset(
    product_id for spec in _MODEL_SPECS for product_id in spec.app_product_ids
)

# Legacy FX gates describe complete app workflows, not command-7 effects. Keep
# them as catalog evidence only until their codecs, defaults and UI semantics
# are independently established.
_MANUAL_FX_PRODUCT_IDS: Final = _ALL_CATALOG_PRODUCT_IDS - _ids(
    "02065 02075 02085 400G5 400H5 400I5 400R5 400S5 400T5 400U5 400V5 "
    "400W5 400X5 400Y5 400Z5 40145 40165 40185 401A5 401B5 401C5 401D5 "
    "401E5 401F5"
)
_PROGRAM_FX_PRODUCT_IDS: Final = _ALL_CATALOG_PRODUCT_IDS - _ids(
    "02065 02075 02085 09005 09015 400G5 400H5 400I5"
)
_PICKER_FX_PRODUCT_IDS: Final = _ALL_CATALOG_PRODUCT_IDS - _ids(
    "00075 00085 02065 02085 09005 09015 400G5 400H5 400I5 400R5 400S5 "
    "40145 40165 401A5 401B5 401C5 401D5 401E5 401F5"
)
_TOUCHBAR_FX_PRODUCT_IDS: Final = _ids("000D5 05010")
_MUSIC_FX_PRODUCT_IDS: Final = _ALL_CATALOG_PRODUCT_IDS - _ids(
    "00075 00085 02065 02075 02085 09005 09015 400G5 400H5 400I5 400R5 "
    "400S5 400V5 40145 40165 401A5 401B5 401C5 401D5 401E5 401F5"
)


# Steady-color sub-capabilities use the app's individual protocol gates. HSI
# and G/M come from the already validated steady family; these sets capture the
# independent RGB/RGBW command-4, XY command-5 and Gel command-3 gates.
_RGB_PRODUCT_IDS: Final = _ids(
    "000F5 000G5 000K5 000L5 000N5 000P5 02005 02035 02065 02085 05010 "
    "07006 07007 07008 09005 09015 400U5 400W5 400X5 40145 40165 40185 401A5"
)
_XY_PRODUCT_IDS: Final = _ids(
    "000F5 000G5 000K5 000L5 000M5 000N5 000P5 000Q5 000R5 000S5 02005 "
    "02035 02065 02075 02085 05010 07006 07007 07008 09005 09015"
)
_GEL_PRODUCT_IDS: Final = _ids(
    "000F5 000G5 000K5 000L5 000N5 000P5 02005 02035 02065 02085 05010 "
    "07006 07007 07008 09005 09015"
)
_ADVANCED_HSI_VERSION_BY_PRODUCT_ID: Final = {
    "000G5": "1.5",
    "000K5": "1.0",
    "000L5": "1.0",
    "000M5": "1.5",
    "000N5": "0.1",
    "000P5": "0.1",
    "000Q5": "1.0",
    "000R5": "0.1",
    "000S5": "1.0",
    "02065": "0.1",
    "02075": "1.0",
    "02085": "0.1",
    "05010": "2.1",
    "07006": "1.8",
    "07007": "1.8",
    "07008": "1.8",
    "09005": "1.0",
    "09015": "1.0",
    "400G5": "3.3",
    "400H5": "3.3",
    "400I5": "3.3",
}
_GM_RANGE_OVERRIDES: Final = {"000J5": (5, 15)}
_GM_V2_VERSION_BY_PRODUCT_ID: Final = {
    "000M5": "1.2",
    "000N5": "0.1",
    "000P5": "0.1",
    "000Q5": "0.1",
    "000R5": "0.1",
    "000S5": "0.1",
    "02065": "0.1",
    "02075": "1.0",
    "02085": "0.1",
    "05010": "2.3",
}


def _system_fx2(name: str, generation: Literal[2, 3]) -> CatalogSystemEffect:
    """Create one entry using the command-34 codec's canonical display name."""
    return CatalogSystemEffect(name, generation)


_SYSTEM_FX2_CLASSIC: Final = (
    _system_fx2("Lightning II", 2),
    _system_fx2("TV II", 2),
    _system_fx2("Fire II", 2),
    _system_fx2("Strobe II", 2),
    _system_fx2("Explosion II", 2),
    _system_fx2("Faulty Bulb II", 2),
    _system_fx2("Pulsing II", 2),
    _system_fx2("Welding II", 2),
    _system_fx2("Cop Car II", 2),
)
_SYSTEM_FX2_MC_PRO: Final = _SYSTEM_FX2_CLASSIC[:-1]
_SYSTEM_FX2_NOVA: Final = (_system_fx2("Party Lights II", 2),)
_SYSTEM_FX2_PIXEL: Final = (
    _system_fx2("Paparazzi II", 2),
    _system_fx2("Party Lights II", 2),
    _system_fx2("Fireworks II", 2),
    _system_fx2("Lightning III", 3),
    _system_fx2("TV III", 3),
    _system_fx2("Fire III", 3),
    _system_fx2("Faulty Bulb III", 3),
    _system_fx2("Pulsing III", 3),
    _system_fx2("Cop Car III", 3),
)
_SYSTEM_FX2_BY_PRODUCT_ID: Final = {
    **{
        product_id: _SYSTEM_FX2_CLASSIC
        for product_id in _ids("000G5 000K5 000L5 000N5 000P5 09005 09015")
    },
    **{product_id: _SYSTEM_FX2_PIXEL for product_id in _ids("000F5 07006 07007 07008")},
    **{product_id: _SYSTEM_FX2_NOVA for product_id in _ids("02065 02075 02085")},
    "05010": _SYSTEM_FX2_MC_PRO,
}


_PIXEL_FX_LINEAR: Final = (
    "Color Fade",
    "Color Cycle",
    "Pixel Fire",
    "One Pixel Chase",
    "Two Pixel Chase",
    "Three Pixel Chase",
    "Rainbow",
)
_PIXEL_FX_SM5C: Final = ("On/Off FX", "Belt FX", "Music")
_PIXEL_FX_BY_PRODUCT_ID: Final = {
    **{
        product_id: _PIXEL_FX_LINEAR
        for product_id in _ids("000F5 07006 07007 07008 400G5 400H5 400I5")
    },
    "400F5": _PIXEL_FX_SM5C,
}
_PIXEL_NUM_BY_PRODUCT_ID: Final = {
    **{
        product_id: 4
        for product_id in _ids("000F5 02075 07006 07007 07008 400G5 400H5 400I5")
    },
    "400F5": 1,
}


_PARTITION_BY_PRODUCT_ID: Final = {
    # The support flag is genuinely false in the app data for NOVA 9, despite
    # the accompanying version and geometry fields. Preserve all fields.
    "02075": CatalogPartitionCapabilities(
        version="1.0", pixel_x1=2, pixel_y1=1, pixel_x2=4, pixel_y2=1
    ),
    "07006": CatalogPartitionCapabilities(
        supported=True,
        version="1.0",
        pixel_x1=4,
        pixel_y1=1,
        pixel_x2=24,
        pixel_y2=1,
        pixel_xy=((4, 1), (8, 1), (12, 1), (24, 1)),
    ),
    "07007": CatalogPartitionCapabilities(
        supported=True,
        version="1.0",
        pixel_x1=4,
        pixel_y1=1,
        pixel_x2=24,
        pixel_y2=1,
        pixel_xy=((4, 1), (8, 1), (12, 1), (16, 1), (24, 1)),
    ),
    "07008": CatalogPartitionCapabilities(
        supported=True,
        version="1.0",
        pixel_x1=4,
        pixel_y1=1,
        pixel_x2=32,
        pixel_y2=1,
        pixel_xy=((4, 1), (8, 1), (12, 1), (16, 1), (24, 1), (32, 1)),
    ),
    "400F5": CatalogPartitionCapabilities(
        supported=True,
        version="1.0",
        pixel_x1=5,
        pixel_y1=1,
        pixel_x2=25,
        pixel_y2=1,
    ),
}
_MULTI_PARTITION_BY_PRODUCT_ID: Final = {
    product_id: CatalogMultiPartitionCapabilities(supported=True, version="1.8")
    for product_id in _ids("07006 07007 07008")
}
_MAGIC_PIXEL_BY_PRODUCT_ID: Final = {
    "07006": CatalogMagicPixelCapabilities(
        supported=True,
        version="1.3",
        pixel=24,
        ppf=8,
        rainbow=True,
        move=True,
        advanced_move=True,
        advanced_move_version="1.7",
        overall=True,
    ),
    "07007": CatalogMagicPixelCapabilities(
        supported=True,
        version="1.3",
        pixel=48,
        ppf=16,
        rainbow=True,
        move=True,
        advanced_move=True,
        advanced_move_version="1.7",
        overall=True,
    ),
    "07008": CatalogMagicPixelCapabilities(
        supported=True,
        version="1.3",
        pixel=96,
        ppf=32,
        rainbow=True,
        move=True,
        advanced_move=True,
        advanced_move_version="1.7",
        overall=True,
    ),
}


_MOTION_BY_PRODUCT_ID: Final = {
    product_id: CatalogMotionCapabilities(supported=True, version="1.0")
    for product_id in _ids("000J5 000K5")
}
_HIGH_SPEED_BY_PRODUCT_ID: Final = {
    "000E5": CatalogHighSpeedPhotographyCapabilities(True, "1.2", 50, 100),
    "000J5": CatalogHighSpeedPhotographyCapabilities(True, "1.0", 20, 100),
    "000K5": CatalogHighSpeedPhotographyCapabilities(True, "1.0", 20, 100),
    "000M5": CatalogHighSpeedPhotographyCapabilities(True, "0.1", 40, 100),
    "000P5": CatalogHighSpeedPhotographyCapabilities(True, "0.1", 5, 100),
    # The app carries a version and range for these two while its support flag
    # remains false. Keeping the flag independent avoids inventing support.
    "000Q5": CatalogHighSpeedPhotographyCapabilities(False, "0.1", 40, 100),
    "000R5": CatalogHighSpeedPhotographyCapabilities(False, "0.1", 40, 100),
    **{
        product_id: CatalogHighSpeedPhotographyCapabilities(True, "1.0", 50, 100)
        for product_id in _ids("40145 40165 40185 401A5")
    },
}
_CCT_EXTENSION_PRODUCT_IDS: Final = _ids("400W5 400X5 40145 40165 40185 401A5")
_THOUSAND_LEVEL_DIMMING_PRODUCT_IDS: Final = _ids(
    "00055 000C5 000D5 000E5 000F5 000G5 000J5 000K5 000L5 000M5 000N5 "
    "000P5 000Q5 000R5 000S5 02035 02065 02085 05010 07006 07007 07008 "
    "09005 09015"
)


def _catalog_capabilities_for_product(
    product_id: str, family: FixtureFamily
) -> FixtureCatalogCapabilities:
    """Build descriptive metadata for one app product UUID."""
    advanced_hsi_version = _ADVANCED_HSI_VERSION_BY_PRODUCT_ID.get(product_id)
    gm_range = (
        _GM_RANGE_OVERRIDES.get(product_id, (0, 20)) if family.supports_gm else (0, 0)
    )
    return FixtureCatalogCapabilities(
        steady_color=CatalogSteadyColorCapabilities(
            hsi=family.supports_color,
            rgb=product_id in _RGB_PRODUCT_IDS,
            xy=product_id in _XY_PRODUCT_IDS,
            gel=product_id in _GEL_PRODUCT_IDS,
            advanced_hsi=advanced_hsi_version is not None,
            advanced_hsi_version=advanced_hsi_version,
            gm=family.supports_gm,
            gm_min=gm_range[0],
            gm_max=gm_range[1],
            gm_v2_version=_GM_V2_VERSION_BY_PRODUCT_ID.get(product_id),
        ),
        system_fx2=_SYSTEM_FX2_BY_PRODUCT_ID.get(product_id, ()),
        pixel_fx=CatalogPixelEffectCapabilities(
            pixel_num=_PIXEL_NUM_BY_PRODUCT_ID.get(product_id, 0),
            effects=_PIXEL_FX_BY_PRODUCT_ID.get(product_id, ()),
        ),
        partition=_PARTITION_BY_PRODUCT_ID.get(product_id, _EMPTY_PARTITION),
        multi_partition=_MULTI_PARTITION_BY_PRODUCT_ID.get(
            product_id, _EMPTY_MULTI_PARTITION
        ),
        magic_pixel=_MAGIC_PIXEL_BY_PRODUCT_ID.get(product_id, _EMPTY_MAGIC_PIXEL),
        motion=_MOTION_BY_PRODUCT_ID.get(product_id, _EMPTY_MOTION),
        high_speed_photography=_HIGH_SPEED_BY_PRODUCT_ID.get(
            product_id, _EMPTY_HIGH_SPEED
        ),
        cct_extension=(
            CatalogCctExtensionCapabilities(True, 1800, 20000)
            if product_id in _CCT_EXTENSION_PRODUCT_IDS
            else _EMPTY_CCT_EXTENSION
        ),
        thousand_level_dimming=(product_id in _THOUSAND_LEVEL_DIMMING_PRODUCT_IDS),
        manual_fx=product_id in _MANUAL_FX_PRODUCT_IDS,
        program_fx=product_id in _PROGRAM_FX_PRODUCT_IDS,
        picker_fx=product_id in _PICKER_FX_PRODUCT_IDS,
        touchbar_fx=product_id in _TOUCHBAR_FX_PRODUCT_IDS,
        music_fx=product_id in _MUSIC_FX_PRODUCT_IDS,
    )


def _profile_from_spec(spec: _ModelSpec) -> FixtureProfile:
    family = FIXTURE_FAMILIES[spec.family]
    boost_min, boost_max = spec.boost or (None, None)
    catalog_capabilities = tuple(
        _catalog_capabilities_for_product(product_id, family)
        for product_id in spec.app_product_ids
    )
    if any(
        capabilities != catalog_capabilities[0]
        for capabilities in catalog_capabilities[1:]
    ):
        raise RuntimeError(f"catalog metadata differs within model {spec.key!r}")
    extension = catalog_capabilities[0].cct_extension
    return FixtureProfile(
        key=spec.key,
        name=spec.name,
        supports_cct=family.supports_cct,
        supports_color=family.supports_color,
        supports_gm=family.supports_gm,
        min_kelvin=extension.min_kelvin if extension.supported else family.min_kelvin,
        max_kelvin=extension.max_kelvin if extension.supported else family.max_kelvin,
        effects=spec.effects,
        supports_boost=spec.boost is not None,
        boost_min_kelvin=boost_min,
        boost_max_kelvin=boost_max,
        supports_fan=spec.fan,
        fan_modes=ALL_FAN_MODES if spec.fan else (),
        family=spec.family,
        manufacturer=spec.manufacturer,
        app_product_ids=spec.app_product_ids,
        aliases=spec.aliases,
        catalog_capabilities=catalog_capabilities[0],
    )


GENERIC_PROFILE: Final = FixtureProfile(
    key=PROFILE_GENERIC,
    name="Generic amaran light",
    supports_cct=True,
    supports_color=False,
    supports_gm=False,
    min_kelvin=DEFAULT_MIN_KELVIN,
    max_kelvin=DEFAULT_MAX_KELVIN,
)

_CATALOG_BY_KEY = {spec.key: _profile_from_spec(spec) for spec in _MODEL_SPECS}

# Ace 25x is the hardware-tested profile. Preserve its narrower verified fan
# mode set and optional diagnostics while sharing the app catalog metadata.
ACE_25X_PROFILE: Final = replace(
    _CATALOG_BY_KEY[PROFILE_ACE_25X],
    fan_modes=("silent", "smart"),
    supports_power=True,
    supports_version=True,
    hardware_tested=True,
)
_CATALOG_BY_KEY[PROFILE_ACE_25X] = ACE_25X_PROFILE

CATALOG_PROFILES: Final = tuple(
    sorted(
        _CATALOG_BY_KEY.values(),
        key=lambda profile: (profile.manufacturer.casefold(), profile.name.casefold()),
    )
)

PROFILES: Final = {
    PROFILE_GENERIC: GENERIC_PROFILE,
    **_CATALOG_BY_KEY,
}


def _normalize_model_identifier(value: str) -> str:
    """Normalize names, slugs and product UUIDs without fuzzy guessing."""
    value = value.casefold().replace("\u00d7", "x")
    return "".join(character for character in value if character.isalnum())


def _build_aliases() -> dict[str, str]:
    aliases: dict[str, str] = {}
    for profile in CATALOG_PROFILES:
        candidates = [
            profile.key,
            profile.name,
            *profile.app_product_ids,
            *profile.aliases,
        ]
        if profile.manufacturer:
            manufacturer = profile.manufacturer.casefold()
            if profile.name.casefold().startswith(f"{manufacturer} "):
                candidates.append(profile.name[len(profile.manufacturer) + 1 :])
            else:
                candidates.append(f"{profile.manufacturer} {profile.name}")

        for candidate in candidates:
            normalized = _normalize_model_identifier(candidate)
            existing = aliases.setdefault(normalized, profile.key)
            if existing != profile.key:
                raise RuntimeError(
                    f"fixture alias {candidate!r} is ambiguous between "
                    f"{existing!r} and {profile.key!r}"
                )
    return aliases


MODEL_ALIASES: Final = _build_aliases()

APP_PRODUCT_ID_TO_PROFILE: Final = {
    product_id.upper(): profile
    for profile in CATALOG_PROFILES
    for product_id in profile.app_product_ids
}


def get_fixture_profile(model: str | None) -> FixtureProfile:
    """Resolve a persisted key, app name or UUID; unknown input stays Generic."""
    if not isinstance(model, str) or not model.strip():
        return GENERIC_PROFILE
    if profile := PROFILES.get(model):
        return profile
    key = MODEL_ALIASES.get(_normalize_model_identifier(model))
    return PROFILES.get(key or DEFAULT_PROFILE, GENERIC_PROFILE)


def get_fixture_profile_by_product_id(product_id: str | None) -> FixtureProfile:
    """Resolve an app product UUID without inferring an unknown fixture."""
    if not isinstance(product_id, str):
        return GENERIC_PROFILE
    return APP_PRODUCT_ID_TO_PROFILE.get(product_id.strip().upper(), GENERIC_PROFILE)


def profile_for_entry(entry: Any) -> FixtureProfile:
    """Resolve the profile selected in a Home Assistant config entry."""
    model = entry.options.get(CONF_MODEL, entry.data.get(CONF_MODEL, DEFAULT_PROFILE))
    return get_fixture_profile(model)
