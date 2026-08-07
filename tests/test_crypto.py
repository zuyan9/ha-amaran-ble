"""Bluetooth Mesh cryptographic toolbox tests."""

from __future__ import annotations

import pytest
from amaranble import crypto
from cryptography.exceptions import InvalidTag


def test_mesh_spec_key_derivation_vectors() -> None:
    """Vectors from Mesh Profile 1.0.1 section 8."""
    net_key = bytes.fromhex("f7a2a44f8e8a8029064f173ddc1e2b00")

    nid, encryption_key, privacy_key = crypto.k2(net_key, b"\x00")

    assert nid == 0x7F
    assert encryption_key.hex() == "9f589181a0f50de73c8070c7a6d27f46"
    assert privacy_key.hex() == "4c715bd4a64b938f99b453351653124f"
    assert crypto.k3(net_key).hex() == "ff046958233db014"
    assert crypto.k4(bytes.fromhex("3216d1509884b533248541792b877f98")) == 0x38


def test_s1_spec_vector() -> None:
    message = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    assert crypto.s1(message).hex() == "8a57896f795cb6abf6867dad41a5fb15"


def test_ccm_round_trip_and_authentication() -> None:
    key = bytes.fromhex("00112233445566778899aabbccddeeff")
    nonce = bytes.fromhex("01000000010002000300000000")
    plaintext = bytes.fromhex("260e00000000000000000e")

    encrypted = crypto.aes_ccm_encrypt(key, nonce, plaintext, 4)

    assert crypto.aes_ccm_decrypt(key, nonce, encrypted, 4) == plaintext
    tampered = encrypted[:-1] + bytes([encrypted[-1] ^ 1])
    with pytest.raises(InvalidTag):
        crypto.aes_ccm_decrypt(key, nonce, tampered, 4)


def test_provisioning_public_key_and_shared_secret() -> None:
    first = crypto.ProvisioningKeyPair()
    second = crypto.ProvisioningKeyPair()

    assert len(first.public_key_bytes) == 64
    assert first.shared_secret(second.public_key_bytes) == second.shared_secret(
        first.public_key_bytes
    )

    with pytest.raises(ValueError, match="64 bytes"):
        first.shared_secret(b"short")
