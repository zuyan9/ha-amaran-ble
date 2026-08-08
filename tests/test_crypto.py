"""Bluetooth Mesh cryptographic toolbox tests."""

from __future__ import annotations

import pytest
from amaranble import crypto
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric import ec


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


def test_mesh_spec_provisioning_security_vector() -> None:
    """Mesh Protocol 1.1 section 8.17.1 (same Mesh 1.0 algorithm)."""
    private = ec.derive_private_key(
        int(
            "06a516693c9aa31a6084545d0c5db641b48572b97203ddffb7ac73f7d0457663",
            16,
        ),
        ec.SECP256R1(),
    )
    keypair = crypto.ProvisioningKeyPair(private)
    device_public = bytes.fromhex(
        "f465e43ff23d3f1b9dc7dfc04da8758"
        "184dbc966204796eccf0d6cf5e16500cc"
        "0201d048bcbbd899eeefc424164e33c2"
        "01c2b010ca6b4d43a8a155cad8ecb279"
    )
    secret = keypair.shared_secret(device_public)
    confirmation_inputs = (
        bytes.fromhex("00")
        + bytes.fromhex("0100010000000000000000")
        + bytes.fromhex("0000000000")
        + keypair.public_key_bytes
        + device_public
    )
    confirmation_salt = crypto.s1(confirmation_inputs)
    confirmation_key = crypto.k1(secret, confirmation_salt, b"prck")
    assert confirmation_salt.hex() == "5faabe187337c71cc6c973369dcaa79a"
    assert confirmation_key.hex() == "e31fe046c68ec339c425fc6629f0336f"

    provisioner_random = bytes.fromhex("8b19ac31d58b124c946209b5db1021b9")
    device_random = bytes.fromhex("55a2a2bca04cd32ff6f346bd0a0c1a3a")
    provisioning_salt = crypto.s1(
        confirmation_salt + provisioner_random + device_random
    )
    session_key = crypto.k1(secret, provisioning_salt, b"prsk")
    session_nonce = crypto.k1(secret, provisioning_salt, b"prsn")[-13:]
    device_key = crypto.k1(secret, provisioning_salt, b"prdk")
    assert provisioning_salt.hex() == "a21c7d45f201cf9489a2fb57145015b4"
    assert session_key.hex() == "c80253af86b33dfa450bbdb2a191fea3"
    assert session_nonce.hex() == "da7ddbe78b5f62b81d6847487e"
    assert device_key.hex() == "0520adad5e0142aa3e325087b4ec16d8"

    data = bytes.fromhex("efb2255e6422d330088e09bb015ed707056700010203040b0c")
    encrypted = crypto.aes_ccm_encrypt(session_key, session_nonce, data, 8)
    assert encrypted.hex() == (
        "d0bd7f4a89a2ff6222af59a90a60ad58acfe3123356f5cec2973e0ec50783b10c7"
    )
