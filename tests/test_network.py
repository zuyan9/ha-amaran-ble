"""Bluetooth Mesh network, transport and access layer tests."""

from __future__ import annotations

import contextlib
import random

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

    with pytest.raises(network.NetworkDecodeError, match="IVI"):
        network.decode_network_pdu(keys, 1, encoded)

    with pytest.raises(network.NetworkDecodeError, match="length"):
        network.decode_network_pdu(keys, 0, encoded + b"too long" * 3)


def test_proxy_configuration_matches_mesh_profile_vector() -> None:
    """Match the Bluetooth Mesh Profile 1.1 Set Filter Type sample."""
    keys = network.NetworkKeys.derive(bytes.fromhex("d1aafb2a1a3c281cbdb0e960edfad852"))

    encoded = network.encode_network_pdu(
        keys,
        iv_index=0x12345678,
        ctl=1,
        ttl=0,
        seq=1,
        src=1,
        dst=network.UNASSIGNED_ADDRESS,
        transport_pdu=bytes.fromhex("0000"),
        proxy_config=True,
    )

    assert encoded.hex() == "10386bd60efbbb8b8c28512e792d3711f4b526"
    assert network.decode_network_pdu(
        keys, 0x12345678, encoded, proxy_config=True
    ) == network.NetworkMessage(
        ctl=1,
        ttl=0,
        seq=1,
        src=1,
        dst=network.UNASSIGNED_ADDRESS,
        transport_pdu=bytes.fromhex("0000"),
    )


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
    first = reassembler.add(2, 1, 0x122345, segments[0])
    second = reassembler.add(2, 1, 0x122346, segments[1])
    complete = reassembler.add(2, 1, 0x122347, segments[2])

    assert first.upper_pdu is None
    assert second.upper_pdu is None
    assert complete.upper_pdu == upper
    assert complete.segment.akf == 1
    assert complete.segment.aid == 0x12
    assert complete.segment.szmic == 0
    assert complete.segment.seq_auth == 0x122345
    assert complete.last_segment_seq == 0x122347

    repeated = reassembler.add(2, 1, 0x122348, segments[0])
    assert repeated.already_complete
    assert repeated.block_ack == 0b111


def test_segment_reassembly_rejects_malformed_and_inconsistent_input() -> None:
    reassembler = network.SegmentReassembler()

    # Formerly reached the completion path with a missing segment and raised
    # KeyError while leaving attacker-controlled state behind.
    with pytest.raises(network.NetworkDecodeError):
        reassembler.add(2, 1, 0x20, bytes.fromhex("8000002078"))
    assert reassembler._pending == {}

    segments = network.build_access_segments(1, 0x12, 0x345, bytes(range(20)))
    reassembler.add(2, 1, 0x345, segments[0])
    changed = bytearray(segments[1])
    changed[0] ^= 0x01  # AID must be stable for the whole transaction.
    with pytest.raises(network.NetworkDecodeError, match="metadata"):
        reassembler.add(2, 1, 0x346, bytes(changed))


def test_segment_reassembly_expires_and_does_not_alias_seqzero() -> None:
    now = [0.0]
    reassembler = network.SegmentReassembler(timeout=10, clock=lambda: now[0])
    old = network.build_access_segments(1, 1, 0x345, bytes(range(20)))
    new = network.build_access_segments(1, 1, 0x345, bytes(range(30, 50)))

    reassembler.add(2, 1, 0x345, old[0])
    # SeqZero repeats after 8192 sequence numbers. Receiving the newer message
    # out of order must not splice its segment onto the abandoned old one.
    result = reassembler.add(2, 1, 0x2346, new[1])
    assert result.upper_pdu is None
    assert result.block_ack == 0b10
    complete = reassembler.add(2, 1, 0x2345, new[0])
    assert complete.upper_pdu == bytes(range(30, 50))

    later = network.build_access_segments(1, 1, 0x456, bytes(range(20)))
    reassembler.add(3, 1, 0x456, later[0])
    now[0] = 11
    reassembler.add(4, 1, 0x456, later[0])
    assert all(key[0] != 3 for key in reassembler._pending)


def test_expired_segmented_transaction_cannot_be_resurrected() -> None:
    now = [0.0]
    reassembler = network.SegmentReassembler(timeout=10, clock=lambda: now[0])
    segments = network.build_access_segments(1, 1, 100, bytes(range(20)))

    reassembler.add(2, 1, 100, segments[0])
    now[0] = 11
    with pytest.raises(network.NetworkDecodeError, match="discarded transaction"):
        reassembler.add(2, 1, 101, segments[1])
    with pytest.raises(network.NetworkDecodeError, match="discarded transaction"):
        reassembler.add(2, 1, 102, segments[0])

    newer = network.build_access_segments(1, 1, 200, bytes(range(20, 40)))
    first = reassembler.add(2, 1, 200, newer[0])
    complete = reassembler.add(2, 1, 201, newer[1])
    assert first.upper_pdu is None
    assert complete.upper_pdu == bytes(range(20, 40))


def test_completed_segment_replay_state_does_not_expire_or_evict() -> None:
    now = [0.0]
    reassembler = network.SegmentReassembler(
        timeout=10,
        max_completed=1,
        clock=lambda: now[0],
    )
    segments = network.build_access_segments(1, 1, 0x345, bytes(range(20)))
    reassembler.add(2, 1, 0x345, segments[0])
    complete = reassembler.add(2, 1, 0x346, segments[1])
    assert complete.upper_pdu == bytes(range(20))

    now[0] = 100
    assert reassembler.completed_ack(2, 1, 0x345) == 0b11
    repeated = reassembler.add(2, 1, 0x347, segments[0])
    assert repeated.already_complete
    assert repeated.block_ack == 0b11

    changed_metadata = bytearray(segments[0])
    changed_metadata[3] = (changed_metadata[3] & 0xE0) | 0x02
    with pytest.raises(network.NetworkDecodeError, match="metadata changed"):
        reassembler.add(2, 1, 0x348, bytes(changed_metadata))

    other = network.build_access_segments(1, 1, 0x400, b"other")
    with pytest.raises(network.NetworkDecodeError, match="tracking capacity"):
        reassembler.add(3, 1, 0x400, other[0])
    assert reassembler.completed_ack(2, 1, 0x345) == 0b11


def test_completed_tracking_capacity_is_reserved_by_pending_transactions() -> None:
    reassembler = network.SegmentReassembler(max_pending=3, max_completed=2)
    segments = network.build_access_segments(1, 1, 1, bytes(range(20)))

    reassembler.add(2, 1, 1, segments[0])
    reassembler.add(3, 1, 1, segments[0])
    with pytest.raises(network.NetworkDecodeError, match="tracking capacity"):
        reassembler.add(4, 1, 1, segments[0])

    reassembler.add(2, 1, 2, segments[1])
    reassembler.add(3, 1, 2, segments[1])
    assert len(reassembler._completed) == 2


def test_segment_reassembly_fuzz_is_bounded_and_never_leaks_exceptions() -> None:
    rng = random.Random(0)
    reassembler = network.SegmentReassembler(max_pending=8)
    for _ in range(5000):
        length = rng.randrange(1, 25)
        lower = bytes([rng.randrange(0x80, 0x100)]) + rng.randbytes(length - 1)
        with contextlib.suppress(network.NetworkDecodeError):
            reassembler.add(
                rng.randrange(1, 0x8000),
                rng.randrange(1, 0x10000),
                rng.randrange(0x1000000),
                lower,
            )
        assert len(reassembler._pending) <= 8


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

    with pytest.raises(network.NetworkDecodeError, match="RFU"):
        network.parse_segment_ack(bytes.fromhex("0d1500000007"))
