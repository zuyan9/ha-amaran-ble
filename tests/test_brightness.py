"""Tests for the shared Home Assistant brightness projection."""

import pytest

from custom_components.amaran_ble.brightness import (
    brightness_to_intensity,
    intensities_have_same_brightness,
    intensity_to_brightness,
)


def test_live_ace_quantization_keeps_the_requested_ha_brightness() -> None:
    """The observed Ace normalization preserves the caller's brightness."""
    assert brightness_to_intensity(128) == 502
    assert intensity_to_brightness(502) == 128
    assert intensity_to_brightness(501) == 128


@pytest.mark.parametrize(
    ("actual", "expected", "equivalent"),
    [
        (501, 502, True),
        (503, 500, True),
        (499, 500, False),
        (504, 503, False),
        (None, None, True),
        (None, 502, False),
        (502, None, False),
    ],
)
def test_intensity_equivalence_uses_ha_brightness_buckets(
    actual: int | None,
    expected: int | None,
    equivalent: bool,
) -> None:
    """Raw distance cannot substitute for equality in HA's 1-255 domain."""
    assert intensities_have_same_brightness(actual, expected) is equivalent
