"""Profile selection and config-entry migration behavior."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("homeassistant")

from custom_components.amaran_ble import async_migrate_entry
from custom_components.amaran_ble.config_flow import (
    _model_selector,
    _options_form_values,
    options_for_profile,
)
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
from custom_components.amaran_ble.profiles import (
    ACE_25X_PROFILE,
    ALL_FAN_MODES,
    APP_PRODUCT_ID_TO_PROFILE,
    CATALOG_PROFILES,
    EMPTY_CATALOG_CAPABILITIES,
    FIXTURE_FAMILIES,
    GENERIC_PROFILE,
    PROFILES,
    CatalogCctExtensionCapabilities,
    CatalogHighSpeedPhotographyCapabilities,
    CatalogMagicPixelCapabilities,
    CatalogMotionCapabilities,
    CatalogMultiPartitionCapabilities,
    CatalogPartitionCapabilities,
    FixtureCatalogCapabilities,
    get_fixture_profile,
    get_fixture_profile_by_product_id,
    profile_for_entry,
)


def _ids(value: str) -> frozenset[str]:
    """Build the immutable product-id sets used by the evidence snapshot."""
    return frozenset(value.split())


_EXPECTED_IDS_BY_FAMILY = {
    "fixed_5600": _ids(
        "00005 00055 00075 000C5 000E5 40025 40045 40065 400L5 400N5 400P5"
    ),
    "cct_2500_7500": _ids("400B5 400D5"),
    "cct_2700_6500": _ids(
        "00045 00085 000D5 40035 40055 40075 400M5 400O5 400Q5 400S5 "
        "400T5 400V5 400Y5 400Z5 40195 401B5 401C5 401D5 401E5 401F5"
    ),
    "cct_3200_6500": frozenset({"40005"}),
    "cct_gm_2500_10000": _ids("000M5 000Q5"),
    "cct_gm_2700_6500": frozenset({"000J5"}),
    "hsi_3200_6500": _ids("05005 05006 05007 05008"),
    "hsi_gm_1800_20000": _ids("000N5 000P5 02065 02075 02085"),
    "hsi_gm_2000_10000": _ids(
        "000F5 000K5 02005 02035 04005 05010 07006 07007 07008 09005 09015"
    ),
    "hsi_gm_2300_10000": _ids("000G5 000L5 400U5 400W5 400X5 40145 40165 40185 401A5"),
    "hsi_gm_2500_10000": _ids("000R5 000S5"),
    "hsi_gm_2500_7500": _ids("40015 40085 40095 400C5 400E5 400J5 400K5 400R5"),
    "hsi_gm_2700_10000": _ids("400G5 400H5 400I5"),
    "hsi_gm_3200_6500": frozenset({"400F5"}),
}

_FX_DAYLIGHT = (
    "off",
    "Fireworks",
    "Faulty Bulb",
    "Lightning",
    "TV",
    "Pulsing",
    "Strobe",
    "Explosion",
    "Paparazzi",
)
_FX_BICOLOR = (*_FX_DAYLIGHT[:-1], "Fire", "Paparazzi")
_FX_VERGE = (
    "off",
    "Fireworks",
    "Faulty Bulb",
    "Lightning",
    "TV",
    "Strobe",
    "Explosion",
    "Fire",
)
_FX_COLOR_CLASSIC = (
    "off",
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
_FX_COLOR_WELDING = (
    "off",
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
_FX_FULL = (
    *_FX_BICOLOR,
    "Club Lights",
    "Candle",
    "Welding",
    "Cop Car",
    "Color Chase",
    "Party Lights",
)

_EXPECTED_IDS_BY_EFFECTS = {
    ("off", "Club Lights", "Candle", "Color Chase"): _ids("02065 02075 02085"),
    _FX_VERGE: _ids("400Y5 400Z5"),
    _FX_DAYLIGHT: _ids(
        "00005 00055 00075 000C5 000E5 40025 40045 40065 400L5 400N5 400P5"
    ),
    _FX_COLOR_CLASSIC: _ids(
        "04005 05005 05006 05007 05008 40015 400C5 400E5 400F5 400G5 "
        "400H5 400I5 400J5 400K5"
    ),
    _FX_BICOLOR: _ids(
        "00045 00085 000D5 000M5 000Q5 000R5 40005 40035 40055 40075 "
        "400B5 400D5 400M5 400O5 400Q5 400S5 400T5 400V5 40195 401B5 "
        "401C5 401D5 401E5 401F5"
    ),
    (*_FX_BICOLOR, "Candle"): frozenset({"000S5"}),
    (*_FX_BICOLOR, "Cop Car"): frozenset({"000J5"}),
    _FX_COLOR_WELDING: _ids("400R5 400U5 400W5 400X5 40145 40165 40185 401A5"),
    _FX_FULL: _ids(
        "000F5 000G5 000K5 000L5 000N5 000P5 02005 02035 05010 07006 "
        "07007 07008 09005 09015 40085 40095"
    ),
}

_FAN_PRODUCT_IDS = _ids(
    "00055 000C5 000D5 000E5 000G5 000J5 000K5 000L5 000M5 000N5 "
    "000P5 000Q5 000R5 000S5 02035 02065 02075 02085 09005 09015 "
    "40005 40015 40025 40035 40045 40055 40065 40075 400J5 400K5 "
    "400L5 400M5 400N5 400O5 400P5 400Q5 400R5 400T5 400U5 400W5 "
    "400X5 40145 40165 40185 40195 401A5 401B5 401C5 401D5 401E5 401F5"
)

_HSI_PRODUCT_IDS = _ids(
    "000F5 000G5 000K5 000L5 000N5 000P5 000R5 000S5 02005 02035 02065 "
    "02075 02085 04005 05005 05006 05007 05008 05010 07006 07007 07008 "
    "09005 09015 40015 40085 40095 400C5 400E5 400F5 400G5 400H5 400I5 "
    "400J5 400K5 400R5 400U5 400W5 400X5 40145 40165 40185 401A5"
)
_RGB_PRODUCT_IDS = _ids(
    "000F5 000G5 000K5 000L5 000N5 000P5 02005 02035 02065 02085 05010 "
    "07006 07007 07008 09005 09015 400U5 400W5 400X5 40145 40165 40185 401A5"
)
_XY_PRODUCT_IDS = _ids(
    "000F5 000G5 000K5 000L5 000M5 000N5 000P5 000Q5 000R5 000S5 02005 "
    "02035 02065 02075 02085 05010 07006 07007 07008 09005 09015"
)
_GEL_PRODUCT_IDS = _ids(
    "000F5 000G5 000K5 000L5 000N5 000P5 02005 02035 02065 02085 05010 "
    "07006 07007 07008 09005 09015"
)
_GM_PRODUCT_IDS = _ids(
    "000F5 000G5 000J5 000K5 000L5 000M5 000N5 000P5 000Q5 000R5 000S5 "
    "02005 02035 02065 02075 02085 04005 05010 07006 07007 07008 09005 "
    "09015 40015 40085 40095 400C5 400E5 400F5 400G5 400H5 400I5 400J5 "
    "400K5 400R5 400U5 400W5 400X5 40145 40165 40185 401A5"
)
_ADVANCED_HSI_VERSIONS = {
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
_GM_V2_VERSIONS = {
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

_SYSTEM_FX2_CLASSIC = (
    ("Lightning II", 2),
    ("TV II", 2),
    ("Fire II", 2),
    ("Strobe II", 2),
    ("Explosion II", 2),
    ("Faulty Bulb II", 2),
    ("Pulsing II", 2),
    ("Welding II", 2),
    ("Cop Car II", 2),
)
_SYSTEM_FX2_PIXEL = (
    ("Paparazzi II", 2),
    ("Party Lights II", 2),
    ("Fireworks II", 2),
    ("Lightning III", 3),
    ("TV III", 3),
    ("Fire III", 3),
    ("Faulty Bulb III", 3),
    ("Pulsing III", 3),
    ("Cop Car III", 3),
)
_EXPECTED_SYSTEM_FX2 = {
    **{
        product_id: _SYSTEM_FX2_CLASSIC
        for product_id in _ids("000G5 000K5 000L5 000N5 000P5 09005 09015")
    },
    **{product_id: _SYSTEM_FX2_PIXEL for product_id in _ids("000F5 07006 07007 07008")},
    **{
        product_id: (("Party Lights II", 2),)
        for product_id in _ids("02065 02075 02085")
    },
    "05010": _SYSTEM_FX2_CLASSIC[:-1],
}

_PIXEL_FX_LINEAR = (
    "Color Fade",
    "Color Cycle",
    "Pixel Fire",
    "One Pixel Chase",
    "Two Pixel Chase",
    "Three Pixel Chase",
    "Rainbow",
)
_EXPECTED_PIXEL_FX = {
    **{
        product_id: _PIXEL_FX_LINEAR
        for product_id in _ids("000F5 07006 07007 07008 400G5 400H5 400I5")
    },
    "400F5": ("On/Off FX", "Belt FX", "Music"),
}
_EXPECTED_PIXEL_NUM = {
    **{
        product_id: 4
        for product_id in _ids("000F5 02075 07006 07007 07008 400G5 400H5 400I5")
    },
    "400F5": 1,
}

_EXPECTED_PARTITION = {
    "02075": CatalogPartitionCapabilities(
        version="1.0", pixel_x1=2, pixel_y1=1, pixel_x2=4, pixel_y2=1
    ),
    "07006": CatalogPartitionCapabilities(
        True,
        "1.0",
        4,
        1,
        24,
        1,
        ((4, 1), (8, 1), (12, 1), (24, 1)),
    ),
    "07007": CatalogPartitionCapabilities(
        True,
        "1.0",
        4,
        1,
        24,
        1,
        ((4, 1), (8, 1), (12, 1), (16, 1), (24, 1)),
    ),
    "07008": CatalogPartitionCapabilities(
        True,
        "1.0",
        4,
        1,
        32,
        1,
        ((4, 1), (8, 1), (12, 1), (16, 1), (24, 1), (32, 1)),
    ),
    "400F5": CatalogPartitionCapabilities(True, "1.0", 5, 1, 25, 1),
}
_EXPECTED_MULTI_PARTITION = {
    product_id: CatalogMultiPartitionCapabilities(True, "1.8")
    for product_id in _ids("07006 07007 07008")
}
_EXPECTED_MAGIC_PIXEL = {
    "07006": CatalogMagicPixelCapabilities(
        True, "1.3", 24, 8, True, True, True, "1.7", True
    ),
    "07007": CatalogMagicPixelCapabilities(
        True, "1.3", 48, 16, True, True, True, "1.7", True
    ),
    "07008": CatalogMagicPixelCapabilities(
        True, "1.3", 96, 32, True, True, True, "1.7", True
    ),
}

_EXPECTED_MOTION = {
    product_id: CatalogMotionCapabilities(True, "1.0")
    for product_id in _ids("000J5 000K5")
}
_EXPECTED_HIGH_SPEED = {
    "000E5": CatalogHighSpeedPhotographyCapabilities(True, "1.2", 50, 100),
    "000J5": CatalogHighSpeedPhotographyCapabilities(True, "1.0", 20, 100),
    "000K5": CatalogHighSpeedPhotographyCapabilities(True, "1.0", 20, 100),
    "000M5": CatalogHighSpeedPhotographyCapabilities(True, "0.1", 40, 100),
    "000P5": CatalogHighSpeedPhotographyCapabilities(True, "0.1", 5, 100),
    "000Q5": CatalogHighSpeedPhotographyCapabilities(False, "0.1", 40, 100),
    "000R5": CatalogHighSpeedPhotographyCapabilities(False, "0.1", 40, 100),
    **{
        product_id: CatalogHighSpeedPhotographyCapabilities(True, "1.0", 50, 100)
        for product_id in _ids("40145 40165 40185 401A5")
    },
}
_CCT_EXTENSION_PRODUCT_IDS = _ids("400W5 400X5 40145 40165 40185 401A5")
_THOUSAND_LEVEL_DIMMING_PRODUCT_IDS = _ids(
    "00055 000C5 000D5 000E5 000F5 000G5 000J5 000K5 000L5 000M5 000N5 "
    "000P5 000Q5 000R5 000S5 02035 02065 02085 05010 07006 07007 07008 "
    "09005 09015"
)
_EXPECTED_CATALOG_PRODUCT_IDS = frozenset().union(*_EXPECTED_IDS_BY_FAMILY.values())
_EXPECTED_LEGACY_FX_IDS = {
    "manual_fx": _EXPECTED_CATALOG_PRODUCT_IDS
    - _ids(
        "02065 02075 02085 400G5 400H5 400I5 400R5 400S5 400T5 400U5 "
        "400V5 400W5 400X5 400Y5 400Z5 40145 40165 40185 401A5 401B5 "
        "401C5 401D5 401E5 401F5"
    ),
    "program_fx": _EXPECTED_CATALOG_PRODUCT_IDS
    - _ids("02065 02075 02085 09005 09015 400G5 400H5 400I5"),
    "picker_fx": _EXPECTED_CATALOG_PRODUCT_IDS
    - _ids(
        "00075 00085 02065 02085 09005 09015 400G5 400H5 400I5 400R5 "
        "400S5 40145 40165 401A5 401B5 401C5 401D5 401E5 401F5"
    ),
    "touchbar_fx": _ids("000D5 05010"),
    "music_fx": _EXPECTED_CATALOG_PRODUCT_IDS
    - _ids(
        "00075 00085 02065 02075 02085 09005 09015 400G5 400H5 400I5 "
        "400R5 400S5 400V5 40145 40165 401A5 401B5 401C5 401D5 401E5 "
        "401F5"
    ),
}
_FIXTURE_CONFIG_REVISION_COUNTS = {
    "00045": 2,
    "00075": 2,
    "00085": 3,
    "000F5": 2,
    "02005": 6,
    "02035": 4,
    "04005": 3,
    "05005": 2,
    "400F5": 2,
}
_EXPECTED_LEGACY_FX_REVISION_COUNTS = {
    "manual_fx": 73,
    "program_fx": 89,
    "picker_fx": 75,
    "touchbar_fx": 2,
    "music_fx": 73,
}


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


def test_catalog_is_an_exhaustive_snapshot_of_app_light_product_ids() -> None:
    """Every fixtureConfig light UUID maps to exactly one static model profile."""
    assert len(FIXTURE_FAMILIES) == 14
    assert len(CATALOG_PROFILES) == 77
    assert len(PROFILES) == 78  # Generic plus 77 app-catalog models.
    assert len(_EXPECTED_CATALOG_PRODUCT_IDS) == 80
    assert frozenset(APP_PRODUCT_ID_TO_PROFILE) == _EXPECTED_CATALOG_PRODUCT_IDS
    assert len({profile.key for profile in CATALOG_PROFILES}) == 77
    assert len({profile.name for profile in CATALOG_PROFILES}) == 77


def test_config_flow_offers_every_catalog_profile_with_honest_labels() -> None:
    """The searchable model picker exposes the full catalog and test status."""
    options = _model_selector().config["options"]

    assert len(options) == len(PROFILES)
    assert options[0] == {"value": PROFILE_GENERIC, "label": "Generic amaran light"}
    assert options[1] == {
        "value": PROFILE_ACE_25X,
        "label": "amaran Ace 25x (hardware-tested)",
    }
    assert {option["value"] for option in options} == set(PROFILES)
    assert all(
        option["value"] in {PROFILE_GENERIC, PROFILE_ACE_25X}
        or option["label"].endswith("(experimental)")
        for option in options
    )


@pytest.mark.parametrize(("family_key", "product_ids"), _EXPECTED_IDS_BY_FAMILY.items())
def test_catalog_steady_capabilities_match_app_families(
    family_key: str, product_ids: frozenset[str]
) -> None:
    """CCT, HSI, G/M and ranges reproduce the app's capability records."""
    family = FIXTURE_FAMILIES[family_key]
    expected = (
        family.supports_cct,
        family.supports_color,
        family.supports_gm,
        family.min_kelvin,
        family.max_kelvin,
    )

    for product_id in product_ids:
        profile = APP_PRODUCT_ID_TO_PROFILE[product_id]
        minimum, maximum = (
            (1800, 20000)
            if product_id in _CCT_EXTENSION_PRODUCT_IDS
            else (family.min_kelvin, family.max_kelvin)
        )
        assert profile.family == family_key
        assert (
            profile.supports_cct,
            profile.supports_color,
            profile.supports_gm,
            profile.min_kelvin,
            profile.max_kelvin,
        ) == (*expected[:3], minimum, maximum)
        assert product_id in profile.app_product_ids


@pytest.mark.parametrize(
    ("expected_effects", "product_ids"), _EXPECTED_IDS_BY_EFFECTS.items()
)
def test_catalog_effects_match_only_unversioned_app_flags(
    expected_effects: tuple[str, ...], product_ids: frozenset[str]
) -> None:
    """Only first-generation systemfx flags become controllable effects."""
    for product_id in product_ids:
        profile = APP_PRODUCT_ID_TO_PROFILE[product_id]
        assert profile.effects == expected_effects
        assert profile.supports_effects
        assert profile.effects[0] == "off"
        assert len(profile.effects) == len(set(profile.effects))

    assert frozenset().union(*_EXPECTED_IDS_BY_EFFECTS.values()) == frozenset(
        APP_PRODUCT_ID_TO_PROFILE
    )


def test_catalog_fan_and_boost_flags_are_evidence_gated() -> None:
    """Fan and Boost appear only where fixtureConfig explicitly enables them."""
    for product_id, profile in APP_PRODUCT_ID_TO_PROFILE.items():
        assert profile.supports_fan is (product_id in _FAN_PRODUCT_IDS)
        if not profile.supports_fan:
            assert profile.fan_modes == ()
        elif profile is ACE_25X_PROFILE:
            assert profile.fan_modes == ("silent", "smart")
        else:
            # The live fan report intersects this wire-format superset before
            # Home Assistant exposes or accepts a mode.
            assert profile.fan_modes == ALL_FAN_MODES

        expected_boost = {
            "400T5": (3800, 5500),
            "400U5": (4000, 5000),
        }.get(product_id)
        assert profile.supports_boost is (expected_boost is not None)
        assert (profile.boost_min_kelvin, profile.boost_max_kelvin) == (
            expected_boost if expected_boost is not None else (None, None)
        )


def test_catalog_steady_color_protocol_metadata_matches_app_fields() -> None:
    """Independent steady-color gates and versions remain exact per product."""
    for product_id, profile in APP_PRODUCT_ID_TO_PROFILE.items():
        color = profile.catalog_capabilities.steady_color
        assert color.hsi is (product_id in _HSI_PRODUCT_IDS)
        assert color.hsi is profile.supports_color
        assert color.rgb is (product_id in _RGB_PRODUCT_IDS)
        assert color.xy is (product_id in _XY_PRODUCT_IDS)
        assert color.gel is (product_id in _GEL_PRODUCT_IDS)
        assert color.advanced_hsi is (product_id in _ADVANCED_HSI_VERSIONS)
        assert color.advanced_hsi_version == _ADVANCED_HSI_VERSIONS.get(product_id)
        assert color.gm is (product_id in _GM_PRODUCT_IDS)
        assert color.gm is profile.supports_gm

        expected_gm_range = (
            (5, 15)
            if product_id == "000J5"
            else (0, 20)
            if product_id in _GM_PRODUCT_IDS
            else (0, 0)
        )
        assert (color.gm_min, color.gm_max) == expected_gm_range
        assert color.gm_v2_version == _GM_V2_VERSIONS.get(product_id)

    assert len(_HSI_PRODUCT_IDS) == 43
    assert len(_RGB_PRODUCT_IDS) == 23
    assert len(_XY_PRODUCT_IDS) == 21
    assert len(_GEL_PRODUCT_IDS) == 16
    assert len(_GM_PRODUCT_IDS) == 42


def test_catalog_cmd34_system_fx2_metadata_matches_app_generation_flags() -> None:
    """SystemFX2 names and II/III generations match every versioned app flag."""
    for product_id, profile in APP_PRODUCT_ID_TO_PROFILE.items():
        actual = tuple(
            (effect.name, effect.generation)
            for effect in profile.catalog_capabilities.system_fx2
        )
        assert actual == _EXPECTED_SYSTEM_FX2.get(product_id, ())
        for name, generation in actual:
            assert name.endswith(" II" if generation == 2 else " III")
            assert name not in profile.effects

    assert len(_EXPECTED_SYSTEM_FX2) == 15
    assert {
        generation
        for effects in _EXPECTED_SYSTEM_FX2.values()
        for _, generation in effects
    } == {
        2,
        3,
    }


def test_only_default_safe_system_fx2_effects_are_runtime_selectable() -> None:
    """Generation III stays cataloged but hidden until app defaults are proven."""
    profile = get_fixture_profile_by_product_id("000F5")
    cataloged = profile.catalog_capabilities.system_fx2

    assert profile.system_effects2 == tuple(
        effect.name for effect in cataloged if effect.generation == 2
    )
    assert all(
        effect.name in profile.all_effects
        for effect in cataloged
        if effect.generation == 2
    )
    assert all(
        effect.name not in profile.all_effects
        for effect in cataloged
        if effect.generation == 3
    )
    assert profile.supports_effects


def test_catalog_cmd33_pixel_fx_metadata_matches_app_fields() -> None:
    """PixelFX names and raw pixel_num values reproduce command-33 gates."""
    for product_id, profile in APP_PRODUCT_ID_TO_PROFILE.items():
        pixel_fx = profile.catalog_capabilities.pixel_fx
        assert pixel_fx.pixel_num == _EXPECTED_PIXEL_NUM.get(product_id, 0)
        assert pixel_fx.effects == _EXPECTED_PIXEL_FX.get(product_id, ())

    assert len(_EXPECTED_PIXEL_NUM) == 9
    assert len(_EXPECTED_PIXEL_FX) == 8


def test_only_defaults_proven_command33_effects_are_runtime_selectable() -> None:
    """Linear pixel programs are exposed, while unrelated SM5c modes stay raw."""
    linear = get_fixture_profile_by_product_id("000F5")
    assert linear.pixel_effects == _EXPECTED_PIXEL_FX["000F5"]
    assert all(effect in linear.all_effects for effect in linear.pixel_effects)
    assert linear.supports_effects

    sm5c = get_fixture_profile_by_product_id("400F5")
    assert sm5c.catalog_capabilities.pixel_fx.effects == (
        "On/Off FX",
        "Belt FX",
        "Music",
    )
    assert sm5c.pixel_effects == ()
    assert all(
        effect not in sm5c.all_effects
        for effect in sm5c.catalog_capabilities.pixel_fx.effects
    )


def test_catalog_partition_multi_and_magic_metadata_matches_app_fields() -> None:
    """Partition geometry and Magic PixelFX gates preserve all app values."""
    empty_partition = CatalogPartitionCapabilities()
    empty_multi = CatalogMultiPartitionCapabilities()
    empty_magic = CatalogMagicPixelCapabilities()

    for product_id, profile in APP_PRODUCT_ID_TO_PROFILE.items():
        catalog = profile.catalog_capabilities
        assert catalog.partition == _EXPECTED_PARTITION.get(product_id, empty_partition)
        assert catalog.multi_partition == _EXPECTED_MULTI_PARTITION.get(
            product_id, empty_multi
        )
        assert catalog.magic_pixel == _EXPECTED_MAGIC_PIXEL.get(product_id, empty_magic)

    # NOVA 9 carries partition metadata while its app support flag is false.
    nova_partition = APP_PRODUCT_ID_TO_PROFILE["02075"].catalog_capabilities.partition
    assert not nova_partition.supported
    assert nova_partition.version == "1.0"
    assert (nova_partition.pixel_x1, nova_partition.pixel_x2) == (2, 4)
    assert all(not value.v2_supported for value in _EXPECTED_PARTITION.values())
    assert all(
        not value.v2_location_supported for value in _EXPECTED_PARTITION.values()
    )
    assert all(
        not value.fire and not value.word for value in _EXPECTED_MAGIC_PIXEL.values()
    )


def test_catalog_motion_high_speed_cct_extension_and_dimming_match_app() -> None:
    """Remaining protocol-family gates retain flags, versions and ranges."""
    empty_motion = CatalogMotionCapabilities()
    empty_high_speed = CatalogHighSpeedPhotographyCapabilities()
    empty_extension = CatalogCctExtensionCapabilities()
    extension = CatalogCctExtensionCapabilities(True, 1800, 20000)

    for product_id, profile in APP_PRODUCT_ID_TO_PROFILE.items():
        catalog = profile.catalog_capabilities
        assert catalog.motion == _EXPECTED_MOTION.get(product_id, empty_motion)
        assert catalog.high_speed_photography == _EXPECTED_HIGH_SPEED.get(
            product_id, empty_high_speed
        )
        assert catalog.cct_extension == (
            extension if product_id in _CCT_EXTENSION_PRODUCT_IDS else empty_extension
        )
        assert catalog.thousand_level_dimming is (
            product_id in _THOUSAND_LEVEL_DIMMING_PRODUCT_IDS
        )

    # Preserve non-zero catalog metadata without converting it into support.
    for product_id in ("000Q5", "000R5"):
        high_speed = APP_PRODUCT_ID_TO_PROFILE[
            product_id
        ].catalog_capabilities.high_speed_photography
        assert not high_speed.supported
        assert (
            high_speed.version,
            high_speed.intensity_min,
            high_speed.intensity_max,
        ) == (
            "0.1",
            40,
            100,
        )


def test_catalog_legacy_fx_flags_match_every_app_record() -> None:
    """Five legacy workflow gates remain exact, immutable catalog evidence."""
    for product_id, profile in APP_PRODUCT_ID_TO_PROFILE.items():
        catalog = profile.catalog_capabilities
        for field, enabled_ids in _EXPECTED_LEGACY_FX_IDS.items():
            assert getattr(catalog, field) is (product_id in enabled_ids)

    assert {
        field: len(product_ids)
        for field, product_ids in _EXPECTED_LEGACY_FX_IDS.items()
    } == {
        "manual_fx": 56,
        "program_fx": 72,
        "picker_fx": 61,
        "touchbar_fx": 2,
        "music_fx": 59,
    }
    assert (
        sum(
            _FIXTURE_CONFIG_REVISION_COUNTS.get(product_id, 1)
            for product_id in _EXPECTED_CATALOG_PRODUCT_IDS
        )
        == 97
    )
    assert {
        field: sum(
            _FIXTURE_CONFIG_REVISION_COUNTS.get(product_id, 1)
            for product_id in product_ids
        )
        for field, product_ids in _EXPECTED_LEGACY_FX_IDS.items()
    } == _EXPECTED_LEGACY_FX_REVISION_COUNTS

    # These workflow gates never become Home Assistant effect names.
    sm5c = get_fixture_profile_by_product_id("400F5")
    assert sm5c.catalog_capabilities.music_fx
    assert "Music" not in sm5c.all_effects


def test_catalog_only_metadata_is_typed_immutable_and_runtime_neutral() -> None:
    """Descriptive app evidence cannot mutate or widen runtime capabilities."""
    catalog = APP_PRODUCT_ID_TO_PROFILE["07006"].catalog_capabilities
    assert isinstance(catalog, FixtureCatalogCapabilities)
    assert isinstance(catalog.system_fx2, tuple)
    assert isinstance(catalog.pixel_fx.effects, tuple)
    assert isinstance(catalog.partition.pixel_xy, tuple)
    assert GENERIC_PROFILE.catalog_capabilities is EMPTY_CATALOG_CAPABILITIES

    with pytest.raises(FrozenInstanceError):
        catalog.thousand_level_dimming = False  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        catalog.steady_color.rgb = False  # type: ignore[misc]

    # Ace's verified runtime profile remains unchanged by catalog-only fields.
    assert ACE_25X_PROFILE.effects == _FX_BICOLOR
    assert ACE_25X_PROFILE.fan_modes == ("silent", "smart")
    assert ACE_25X_PROFILE.catalog_capabilities.system_fx2 == ()


def test_only_hardware_tested_ace_enables_unproven_optional_diagnostics() -> None:
    """The broad catalog must not imply power/version protocol verification."""
    for profile in CATALOG_PROFILES:
        if profile is ACE_25X_PROFILE:
            assert profile.hardware_tested
            assert profile.supports_power
            assert profile.supports_version
        else:
            assert not profile.hardware_tested
            assert not profile.supports_power
            assert not profile.supports_version


def test_every_static_alias_and_product_uuid_resolves_to_one_profile() -> None:
    """Persisted keys, catalog names, brand aliases and UUIDs are equivalent."""
    for profile in CATALOG_PROFILES:
        identifiers = (
            profile.key,
            profile.name,
            *profile.aliases,
            *profile.app_product_ids,
        )
        for identifier in identifiers:
            assert get_fixture_profile(identifier) is profile
            assert get_fixture_profile(f"  {identifier.swapcase()}  ") is profile

        if profile.name.casefold().startswith("amaran "):
            assert get_fixture_profile(profile.name[7:]) is profile
        else:
            assert get_fixture_profile(f"Aputure {profile.name}") is profile

        for product_id in profile.app_product_ids:
            assert get_fixture_profile_by_product_id(product_id.lower()) is profile

    assert get_fixture_profile("Aputure NOVA 9° 2\u00d71").key == "nova_9_2x1"
    assert get_fixture_profile("LS 300d II").key == "ls_c300d_ii"
    assert get_fixture_profile("amaran 60x").key == "cob_60x"


@pytest.mark.parametrize(
    "product_id",
    [
        # Product-list records with no bundled light capability record.
        "00015",
        "00025",
        "00035",
        "00065",
        "00095",
        "000B5",
        "000I5",
        "20005",
        "20015",
        "20025",
        "20035",
        "400A5",
        # Non-light accessories are never inferred as fixtures.
        "40105",
        "40115",
        "EM005",
        "EM015",
    ],
)
def test_products_without_light_capability_records_stay_generic(
    product_id: str,
) -> None:
    assert get_fixture_profile(product_id) is GENERIC_PROFILE
    assert get_fixture_profile_by_product_id(product_id) is GENERIC_PROFILE


@pytest.mark.parametrize("model", [None, "", "future_model", "DEADBEEF"])
def test_unknown_model_identifiers_stay_on_safe_generic_profile(
    model: str | None,
) -> None:
    assert get_fixture_profile(model) is GENERIC_PROFILE


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("100d", (False, False, False, 5600, 5600)),
        ("f21x", (True, False, False, 2500, 7500)),
        ("ace_25c", (True, True, True, 2300, 10000)),
        ("storm_80c", (True, True, True, 1800, 20000)),
    ],
)
def test_catalog_profiles_override_manual_capability_input(
    model: str, expected: tuple[bool, bool, bool, int, int]
) -> None:
    """A selected catalog model cannot be widened by stale manual options."""
    options = options_for_profile(
        {
            CONF_MODEL: model,
            CONF_SUPPORTS_CCT: False,
            CONF_SUPPORTS_COLOR: False,
            CONF_SUPPORTS_GM: False,
            CONF_MIN_KELVIN: 800,
            CONF_MAX_KELVIN: 20000,
        }
    )

    assert (
        options[CONF_SUPPORTS_CCT],
        options[CONF_SUPPORTS_COLOR],
        options[CONF_SUPPORTS_GM],
        options[CONF_MIN_KELVIN],
        options[CONF_MAX_KELVIN],
    ) == expected


def test_entry_lookup_accepts_legacy_data_alias_but_options_take_precedence() -> None:
    """Data aliases migrate naturally while an explicit option remains authoritative."""
    legacy = SimpleNamespace(options={}, data={CONF_MODEL: "amaran Ace 25c"})
    overridden = SimpleNamespace(
        options={CONF_MODEL: "future_model"},
        data={CONF_MODEL: "amaran Ace 25c"},
    )

    assert profile_for_entry(legacy).key == "ace_25c"
    assert profile_for_entry(overridden) is GENERIC_PROFILE


def test_profile_options_canonicalize_aliases_and_legacy_data_models() -> None:
    """Configure keeps a legacy named profile instead of silently using Generic."""
    alias_options = options_for_profile({CONF_MODEL: "amaran Ace 25c"})
    assert alias_options == {
        CONF_MODEL: "ace_25c",
        CONF_SUPPORTS_CCT: True,
        CONF_SUPPORTS_COLOR: True,
        CONF_SUPPORTS_GM: True,
        CONF_MIN_KELVIN: 2300,
        CONF_MAX_KELVIN: 10000,
    }

    entry = SimpleNamespace(
        options={},
        data={CONF_MODEL: "amaran Ace 25c"},
    )
    assert _options_form_values(entry) == alias_options


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
