"""Bluetooth Mesh cryptographic primitives.

Implements the key derivation and encryption functions from the Bluetooth Mesh
Profile specification v1.0.1, section 3.8.2 ("Security toolbox").

Everything here is pure computation with no I/O, which makes it straightforward
to check against the spec's sample data — see ``tests/test_crypto.py``.
"""

from __future__ import annotations

import os

from cryptography.hazmat.primitives import cmac
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESCCM

ZERO16 = b"\x00" * 16


def aes_ecb(key: bytes, block: bytes) -> bytes:
    """Raw single-block AES-128 in ECB mode (the spec's ``e`` function)."""
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return encryptor.update(block) + encryptor.finalize()


def aes_cmac(key: bytes, message: bytes) -> bytes:
    """AES-CMAC-128 (RFC 4493)."""
    c = cmac.CMAC(algorithms.AES(key))
    c.update(message)
    return c.finalize()


def s1(m: bytes) -> bytes:
    """Salt generation function: ``s1(M) = AES-CMAC(0, M)``."""
    return aes_cmac(ZERO16, m)


def k1(n: bytes, salt: bytes, p: bytes) -> bytes:
    """Derivation function ``k1(N, SALT, P) = AES-CMAC(AES-CMAC(SALT, N), P)``."""
    return aes_cmac(aes_cmac(salt, n), p)


def k2(n: bytes, p: bytes) -> tuple[int, bytes, bytes]:
    """Network key material: returns ``(NID, EncryptionKey, PrivacyKey)``.

    ``p`` is ``b"\\x00"`` for master security material, which is all we use --
    friendship material would need the friend/LPN addresses instead.
    """
    salt = s1(b"smk2")
    t = aes_cmac(salt, n)
    t1 = aes_cmac(t, p + b"\x01")
    t2 = aes_cmac(t, t1 + p + b"\x02")
    t3 = aes_cmac(t, t2 + p + b"\x03")
    return t1[15] & 0x7F, t2, t3


def k3(n: bytes) -> bytes:
    """Network ID: the 8-byte value advertised in proxy/beacon packets."""
    t = aes_cmac(s1(b"smk3"), n)
    return aes_cmac(t, b"id64\x01")[8:]


def k4(n: bytes) -> int:
    """Application key identifier (AID), 6 bits."""
    t = aes_cmac(s1(b"smk4"), n)
    return aes_cmac(t, b"id6\x01")[15] & 0x3F


def aes_ccm_encrypt(key: bytes, nonce: bytes, plaintext: bytes, mic_len: int) -> bytes:
    """AES-CCM encrypt with no additional data; returns ciphertext || MIC."""
    return AESCCM(key, tag_length=mic_len).encrypt(nonce, plaintext, None)


def aes_ccm_decrypt(key: bytes, nonce: bytes, data: bytes, mic_len: int) -> bytes:
    """AES-CCM decrypt; raises ``cryptography.exceptions.InvalidTag`` on a bad MIC."""
    return AESCCM(key, tag_length=mic_len).decrypt(nonce, data, None)


# ─── Nonces (spec 3.8.5) ─────────────────────────────────────────────────────
#
# Every nonce is 13 bytes and starts with a type byte that domain-separates the
# four layers, so the same key can never be used to encrypt two different kinds
# of payload under the same counter.

NONCE_NETWORK = 0x00
NONCE_APPLICATION = 0x01
NONCE_DEVICE = 0x02
NONCE_PROXY = 0x03


def network_nonce(ctl: int, ttl: int, seq: int, src: int, iv_index: int) -> bytes:
    return bytes([NONCE_NETWORK, (ctl << 7) | (ttl & 0x7F)]) + (
        seq.to_bytes(3, "big")
        + src.to_bytes(2, "big")
        + b"\x00\x00"
        + iv_index.to_bytes(4, "big")
    )


def proxy_nonce(seq: int, src: int, iv_index: int) -> bytes:
    return bytes([NONCE_PROXY, 0x00]) + (
        seq.to_bytes(3, "big")
        + src.to_bytes(2, "big")
        + b"\x00\x00"
        + iv_index.to_bytes(4, "big")
    )


def transport_nonce(
    nonce_type: int, szmic: int, seq: int, src: int, dst: int, iv_index: int
) -> bytes:
    """Application (0x01) or device (0x02) nonce -- identical layout."""
    return bytes([nonce_type, (szmic & 1) << 7]) + (
        seq.to_bytes(3, "big")
        + src.to_bytes(2, "big")
        + dst.to_bytes(2, "big")
        + iv_index.to_bytes(4, "big")
    )


# ─── Network header obfuscation (spec 3.8.7.3) ───────────────────────────────


def obfuscation_ecb(privacy_key: bytes, iv_index: int, privacy_random: bytes) -> bytes:
    """The PECB used to XOR-mask the CTL/TTL/SEQ/SRC network header bytes."""
    plaintext = b"\x00" * 5 + iv_index.to_bytes(4, "big") + privacy_random[:7]
    return aes_ecb(privacy_key, plaintext)[:6]


# ─── ECDH P-256 (provisioning) ───────────────────────────────────────────────


class ProvisioningKeyPair:
    """An ephemeral P-256 key pair, exchanged as raw 64-byte X||Y coordinates."""

    def __init__(self, private_key: ec.EllipticCurvePrivateKey | None = None) -> None:
        self._key = private_key or ec.generate_private_key(ec.SECP256R1())

    @property
    def public_key_bytes(self) -> bytes:
        """Uncompressed public key without the 0x04 prefix, as mesh sends it."""
        numbers = self._key.public_key().public_numbers()
        return numbers.x.to_bytes(32, "big") + numbers.y.to_bytes(32, "big")

    def shared_secret(self, peer_public_key: bytes) -> bytes:
        """ECDH shared secret -- the X coordinate only, per the mesh spec."""
        if len(peer_public_key) != 64:
            raise ValueError("peer public key must be 64 bytes (X||Y)")
        peer = ec.EllipticCurvePublicNumbers(
            int.from_bytes(peer_public_key[:32], "big"),
            int.from_bytes(peer_public_key[32:], "big"),
            ec.SECP256R1(),
        ).public_key()
        return self._key.exchange(ec.ECDH(), peer)


def random_bytes(n: int) -> bytes:
    return os.urandom(n)
