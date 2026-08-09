"""Shared Home Assistant and fixture brightness conversions."""

from __future__ import annotations

HA_MAX_BRIGHTNESS = 255
MAX_INTENSITY = 1000


def brightness_to_intensity(brightness: int) -> int:
    """Convert Home Assistant's 1-255 brightness to fixture intensity."""
    return max(1, round(brightness / HA_MAX_BRIGHTNESS * MAX_INTENSITY))


def intensity_to_brightness(intensity: int) -> int:
    """Project a fixture intensity into Home Assistant's brightness domain."""
    # The proprietary field is ten bits wide, although the app's normal UI
    # range ends at 1000. Keep unusual but valid reports inside HA's contract.
    return max(
        1,
        min(
            HA_MAX_BRIGHTNESS,
            round(intensity / MAX_INTENSITY * HA_MAX_BRIGHTNESS),
        ),
    )


def intensities_have_same_brightness(actual: int | None, expected: int | None) -> bool:
    """Return whether two raw values represent the same HA brightness.

    Ace 25x firmware has been observed applying raw intensity 502 as 501 in an
    otherwise exact status report. Both values represent brightness 128 to the
    caller. Compare in the user-facing domain so harmless device quantization
    confirms, while a report that would publish another brightness is rejected.
    """
    if actual is None or expected is None:
        return actual is expected
    return intensity_to_brightness(actual) == intensity_to_brightness(expected)
