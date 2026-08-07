"""Bluetooth Mesh network, transport and access layer tests."""

from __future__ import annotations

import pytest
from amaranble import crypto, network

NET_KEY = bytes.fromhex("f7a2a44f8e8a8029064f173ddc1e2b00")
APP_KEY = bytes.fromhex("3216d1509884b533248541792b877f98")


def test_network_pdu_round_trip() -> None:
    keys = network.NetworkKeys.derive(NET_KEY)
    transport = bytes.fromhex("6639d4c1a4f48a9f")

    encoded = network.encode_network_pdu(
        keys,
        iv_index=0x12345678,
        ctl=0,
        ttl=5,
        seq=0x3129AB,
        src=0x0001,
        dst=0x0002,
        transport_pdu=transport,
    )
    decoded = network.decode_network_pdu(keys, 0x12345678, encoded)

    assert decoded == network.NetworkMessage(
        ctl=0,
        ttl=5,
        seq=0x3129AB,
        src=0x0001,
        dst=0x0002,
        transport_pdu=transport,
    )


def test_network_rejects_wrong_key_and_tampering() -> None:
    keys = network.NetworkKeys.derive(NET_KEY)
    encoded = network.encode_network_pdu(
        keys,
        iv_index=0,
        ctl=0,
        ttl=5,
        seq=7,
        src=1,
        dst=2,
        transport_pdu=b"payload",
    )

    with pytest.raises(network.NetworkDecodeError):
        network.decode_network_pdu(network.NetworkKeys.derive(b"x" * 16), 0, encoded)

    tampered = encoded[:-1] + bytes([encoded[-1] ^ 1])
    with pytest.raises(network.NetworkDecodeError, match="MIC"):
        network.decode_network_pdu(keys, 0, tampered)


@pytest.mark.parametrize("device_key", [False, True])
def test_upper_transport_round_trip(device_key: bool) -> None:
    access = bytes.fromhex("268d00000000000000018c")
    nonce_type = crypto.NONCE_DEVICE if device_key else crypto.NONCE_APPLICATION
    assert nonce_type in (crypto.NONCE_DEVICE, crypto.NONCE_APPLICATION)

    encrypted = network.encrypt_access_payload(
        APP_KEY,
        device_key=device_key,
        iv_index=0,
        seq=42,
        src=1,
        dst=2,
        access_pdu=access,
    )

    assert (
        network.decrypt_access_payload(
            APP_KEY,
            device_key=device_key,
            iv_index=0,
            seq=42,
            src=1,
            dst=2,
            upper_pdu=encrypted,
        )
        == access
    )


def test_segment_build_and_reassembly() -> None:
    upper = bytes(range(30))
    segments = network.build_access_segments(1, 0x12, 0x345, upper)
    assert len(segments) == 3

    reassembler = network.SegmentReassembler()
    assert reassembler.add(2, 0x122345, segments[0]) is None
    assert reassembler.add(2, 0x122346, segments[1]) is None
    complete = reassembler.add(2, 0x122347, segments[2])

    assert complete == (upper, 1, 0x12, 0, 0x122345)


@pytest.mark.parametrize(
    ("opcode", "encoded"),
    [(0x26, "26"), (0x8202, "8202"), (0xC21102, "c21102")],
)
def test_opcode_round_trip(opcode: int, encoded: str) -> None:
    payload = network.encode_opcode(opcode) + b"params"
    assert payload.hex().startswith(encoded)
    assert network.decode_opcode(payload) == (opcode, b"params")


def test_segment_ack() -> None:
    ack = network.parse_segment_ack(bytes.fromhex("0d1400000007"))
    assert ack.seq_zero == 0x345
    assert ack.block_ack == 0x07
    assert ack.acknowledges_all(3)
    assert not ack.acknowledges_all(4)
