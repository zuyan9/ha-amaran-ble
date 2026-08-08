"""Golden vectors for amaran's Telink opcode 0x26 payload."""

from __future__ import annotations

import pytest
from amaranble import telink


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (telink.status_request(), "0e00000000000000000e"),
        (telink.onoff(False), "8c00000000000000008c"),
        (telink.onoff(True), "8d00000000000000018c"),
        (telink.brightness(0), "8f00000000000000008f"),
        (telink.brightness(1), "cf00000000000040008f"),
        (telink.brightness(500), "0c000000000000007d8f"),
        (telink.brightness(1000), "8900000000000000fa8f"),
        (telink.cct(2700, 1), "f30000000040e1500082"),
        (telink.cct(5600, 800), "ae00000000400123c882"),
        (telink.cct(6500, 1000), "850000000040a128fa82"),
        (telink.cct(10000, 500), "fe0000000040813e7d82"),
        (telink.cct(10010, 500), "54000000004411007d82"),
        (telink.cct(15000, 500), "a30000000044411f7d82"),
        (telink.cct(20000, 500), "020000000044813e7d82"),
        (telink.hsi(45, 60, 800), "fd0000000000af05c881"),
        (telink.hsi(360, 100, 1000), "c10000000000192dfa81"),
    ],
)
def test_command_vectors(payload: bytes, expected: str) -> None:
    assert payload.hex() == expected
    assert payload[0] == sum(payload[1:]) & 0xFF


def _as_report(command: bytes, *, on: bool) -> bytes:
    payload = bytearray(command)
    payload[9] &= 0x7F
    payload[1] = (payload[1] & ~1) | on
    payload[0] = sum(payload[1:]) & 0xFF
    return bytes(payload)


def test_decode_cct_status() -> None:
    state = telink.decode_status(_as_report(telink.cct(5600, 800, -4), on=True))
    assert state == telink.LightState(
        on=True,
        is_hsi=False,
        intensity=800,
        kelvin=5600,
        gm=-4,
        hue=0,
        saturation=0,
    )


@pytest.mark.parametrize("kelvin", [10000, 10010, 15000, 20000])
def test_decode_high_cct_status(kelvin: int) -> None:
    state = telink.decode_status(_as_report(telink.cct(kelvin, 500), on=True))
    assert state is not None
    assert state.kelvin == kelvin


def test_decode_hsi_status() -> None:
    state = telink.decode_status(_as_report(telink.hsi(315, 73, 421), on=False))
    assert state == telink.LightState(
        on=False,
        is_hsi=True,
        intensity=421,
        kelvin=0,
        gm=0,
        hue=315,
        saturation=73,
    )


def test_bounds_are_clamped() -> None:
    assert telink.brightness(-1) == telink.brightness(0)
    assert telink.brightness(1001) == telink.brightness(1000)
    assert telink.cct(0, 500) == telink.cct(telink.MIN_KELVIN, 500)
    assert telink.cct(50000, 500) == telink.cct(telink.MAX_KELVIN, 500)
    assert telink.hsi(-1, -1, -1) == telink.hsi(0, 0, 0)


def test_command_values_use_javascript_half_up_rounding() -> None:
    assert telink.brightness(0.5) == telink.brightness(1)
    assert telink.cct(5600, 500.5, 0.5) == telink.cct(5600, 501, 1)
    assert telink.cct(5600, 500, -1.5) == telink.cct(5600, 500, -1)
    assert telink.hsi(44.5, 60.5, 800.5) == telink.hsi(45, 61, 801)


def test_decode_rejects_invalid_or_non_state_payloads() -> None:
    invalid = bytearray(_as_report(telink.cct(5000, 500), on=True))
    invalid[3] ^= 1
    assert telink.decode_status(bytes(invalid)) is None
    assert telink.decode_status(b"short") is None

    diagnostic = bytearray(10)
    diagnostic[9] = 0x0A
    diagnostic[0] = 0x0A
    assert telink.decode_status(bytes(diagnostic)) is None
