"""PB-GATT provisioning client (Mesh Profile 1.0.1, section 5.4).

Brings an unprovisioned device into a network we control: we generate the
NetKey/AppKey ourselves, so nothing here depends on the vendor's app or on
extracting keys from its database.

Only the ``BTM_ECDH_P256_CMAC_AES128_AES_CCM`` algorithm and the "No OOB"
authentication method are implemented -- that is what amaran fixtures offer,
and it is the one combination every Mesh 1.0 device must support.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from . import crypto
from .gatt import (
    PROVISIONING_DATA_IN,
    PROVISIONING_DATA_OUT,
    TYPE_PROVISIONING,
    MeshGattTransport,
)

_LOGGER = logging.getLogger(__name__)

INVITE = 0x00
CAPABILITIES = 0x01
START = 0x02
PUBLIC_KEY = 0x03
INPUT_COMPLETE = 0x04
CONFIRMATION = 0x05
RANDOM = 0x06
DATA = 0x07
COMPLETE = 0x08
FAILED = 0x09

ALGORITHM_CMAC_AES128 = 0x00
ALGORITHM_HMAC_SHA256 = 0x01

AUTH_METHOD_NO_OOB = 0x00
PUBLIC_KEY_NO_OOB = 0x00

FAILURE_REASONS = {
    0x01: "prohibited",
    0x02: "invalid PDU",
    0x03: "invalid format",
    0x04: "unexpected PDU",
    0x05: "confirmation failed",
    0x06: "out of resources",
    0x07: "decryption failed",
    0x08: "unexpected error",
    0x09: "cannot assign addresses",
    0x0A: "invalid data",
}


class ProvisioningError(Exception):
    """Provisioning could not be completed."""


@dataclass(frozen=True)
class Capabilities:
    num_elements: int
    algorithms: int
    public_key_type: int
    static_oob_type: int
    output_oob_size: int
    output_oob_action: int
    input_oob_size: int
    input_oob_action: int

    @classmethod
    def parse(cls, data: bytes) -> Capabilities:
        if len(data) < 11:
            raise ProvisioningError(f"short capabilities PDU: {data.hex()}")
        return cls(
            num_elements=data[0],
            algorithms=int.from_bytes(data[1:3], "big"),
            public_key_type=data[3],
            static_oob_type=data[4],
            output_oob_size=data[5],
            output_oob_action=int.from_bytes(data[6:8], "big"),
            input_oob_size=data[8],
            input_oob_action=int.from_bytes(data[9:11], "big"),
        )

    @property
    def supports_cmac_aes128(self) -> bool:
        return bool(self.algorithms & (1 << ALGORITHM_CMAC_AES128))


@dataclass(frozen=True)
class ProvisioningResult:
    """What the caller needs to keep in order to talk to the node afterwards."""

    unicast_address: int
    num_elements: int
    device_key: bytes


class Provisioner:
    """Drives one provisioning session over an already-connected bleak client."""

    def __init__(self, client) -> None:
        self._client = client
        self._queue: asyncio.Queue[tuple[int, bytes]] = asyncio.Queue()
        self._transport = MeshGattTransport(
            client, PROVISIONING_DATA_IN, PROVISIONING_DATA_OUT, self._on_message
        )

    def _on_message(self, msg_type: int, payload: bytes) -> None:
        if msg_type != TYPE_PROVISIONING or not payload:
            return
        self._queue.put_nowait((payload[0] & 0x3F, payload[1:]))

    async def _send(self, pdu_type: int, params: bytes = b"") -> None:
        await self._transport.send(TYPE_PROVISIONING, bytes([pdu_type]) + params)

    async def _expect(self, pdu_type: int, timeout: float = 20.0) -> bytes:
        while True:
            try:
                got_type, params = await asyncio.wait_for(
                    self._queue.get(), timeout=timeout
                )
            except TimeoutError:
                raise ProvisioningError(
                    f"timed out waiting for provisioning PDU {pdu_type:#04x}"
                ) from None
            if got_type == FAILED:
                reason = FAILURE_REASONS.get(
                    params[0] if params else 0, f"unknown ({params.hex()})"
                )
                raise ProvisioningError(f"device rejected provisioning: {reason}")
            if got_type == pdu_type:
                return params
            _LOGGER.debug(
                "ignoring provisioning PDU %#04x while awaiting %#04x",
                got_type,
                pdu_type,
            )

    async def provision(
        self,
        network_key: bytes,
        unicast_address: int,
        *,
        key_index: int = 0,
        iv_index: int = 0,
        flags: int = 0,
        attention_duration: int = 0,
        before_commit: Callable[[bytes, int], Awaitable[None]] | None = None,
    ) -> ProvisioningResult:
        """Run the full exchange and return the node's address and device key."""
        await self._transport.start()

        # 1. Invite. Its single parameter byte feeds the confirmation inputs, so
        #    it has to be kept verbatim rather than re-derived later.
        invite_params = bytes([attention_duration])
        await self._send(INVITE, invite_params)

        # 2. Capabilities.
        capabilities_params = await self._expect(CAPABILITIES)
        capabilities = Capabilities.parse(capabilities_params)
        _LOGGER.debug("device capabilities: %s", capabilities)
        if not capabilities.supports_cmac_aes128:
            raise ProvisioningError(
                "device does not support the CMAC-AES128 provisioning algorithm "
                f"(algorithms bitfield {capabilities.algorithms:#06x})"
            )
        if capabilities.num_elements < 1:
            raise ProvisioningError("device reported zero elements")

        # 3. Start: no OOB anywhere, both keys exchanged in-band.
        start_params = bytes(
            [
                ALGORITHM_CMAC_AES128,
                PUBLIC_KEY_NO_OOB,
                AUTH_METHOD_NO_OOB,
                0x00,  # authentication action
                0x00,  # authentication size
            ]
        )
        await self._send(START, start_params)

        # 4/5. Public key exchange.
        keypair = crypto.ProvisioningKeyPair()
        provisioner_public_key = keypair.public_key_bytes
        await self._send(PUBLIC_KEY, provisioner_public_key)
        device_public_key = await self._expect(PUBLIC_KEY)
        if len(device_public_key) != 64:
            raise ProvisioningError(
                f"bad device public key length {len(device_public_key)}"
            )

        ecdh_secret = keypair.shared_secret(device_public_key)

        confirmation_inputs = (
            invite_params
            + capabilities_params[:11]
            + start_params
            + provisioner_public_key
            + device_public_key
        )
        confirmation_salt = crypto.s1(confirmation_inputs)
        confirmation_key = crypto.k1(ecdh_secret, confirmation_salt, b"prck")
        auth_value = b"\x00" * 16  # No OOB

        # 6/7. Confirmation exchange.
        provisioner_random = crypto.random_bytes(16)
        await self._send(
            CONFIRMATION,
            crypto.aes_cmac(confirmation_key, provisioner_random + auth_value),
        )
        device_confirmation = await self._expect(CONFIRMATION)

        # 8/9. Random exchange, then check the device committed to its random.
        await self._send(RANDOM, provisioner_random)
        device_random = await self._expect(RANDOM)
        expected = crypto.aes_cmac(confirmation_key, device_random + auth_value)
        if expected != device_confirmation:
            raise ProvisioningError(
                "device confirmation mismatch - the exchange was tampered with "
                "or the device is speaking a different provisioning variant"
            )

        # 10. Session keys, then the encrypted network credentials.
        provisioning_salt = crypto.s1(
            confirmation_salt + provisioner_random + device_random
        )
        session_key = crypto.k1(ecdh_secret, provisioning_salt, b"prsk")
        session_nonce = crypto.k1(ecdh_secret, provisioning_salt, b"prsn")[-13:]
        device_key = crypto.k1(ecdh_secret, provisioning_salt, b"prdk")

        # Once Provisioning Data is accepted, only these generated keys can
        # recover or reset the node. Give the caller a durable pre-commit hook.
        if before_commit is not None:
            await before_commit(device_key, capabilities.num_elements)

        provisioning_data = (
            network_key
            + key_index.to_bytes(2, "big")
            + bytes([flags])
            + iv_index.to_bytes(4, "big")
            + unicast_address.to_bytes(2, "big")
        )
        await self._send(
            DATA,
            crypto.aes_ccm_encrypt(session_key, session_nonce, provisioning_data, 8),
        )

        # 11. Complete.
        await self._expect(COMPLETE)
        await self._transport.stop()

        _LOGGER.info(
            "provisioned node at %#06x with %d element(s)",
            unicast_address,
            capabilities.num_elements,
        )
        return ProvisioningResult(
            unicast_address=unicast_address,
            num_elements=capabilities.num_elements,
            device_key=device_key,
        )
