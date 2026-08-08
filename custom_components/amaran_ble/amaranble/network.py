"""Mesh network and transport layer framing (Mesh Profile 1.0.1, sections 3.4-3.6).

Pure encode/decode of the layers between an access message and the bytes that
go into a Proxy PDU. No I/O, so it can be exercised without a fixture present.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag

from . import crypto

UNASSIGNED_ADDRESS = 0x0000
ALL_NODES_ADDRESS = 0xFFFF

# Lower transport control opcodes we care about.
CONTROL_OPCODE_SEGMENT_ACK = 0x00

# Access payload bytes that fit in an unsegmented message, once the 4-byte
# TransMIC is accounted for.
UNSEGMENTED_ACCESS_MAX = 11
SEGMENT_PAYLOAD_SIZE = 12
SEGMENT_REASSEMBLY_TIMEOUT = 10.0
MAX_PENDING_REASSEMBLIES = 32
MAX_COMPLETED_REASSEMBLIES = 64


class NetworkDecodeError(Exception):
    """A received PDU was not addressed to this network, or failed its MIC."""


@dataclass(frozen=True)
class NetworkKeys:
    """Everything derived from a single NetKey."""

    net_key: bytes
    nid: int
    encryption_key: bytes
    privacy_key: bytes
    network_id: bytes
    identity_key: bytes
    beacon_key: bytes

    @classmethod
    def derive(cls, net_key: bytes) -> NetworkKeys:
        nid, encryption_key, privacy_key = crypto.k2(net_key, b"\x00")
        return cls(
            net_key=net_key,
            nid=nid,
            encryption_key=encryption_key,
            privacy_key=privacy_key,
            network_id=crypto.k3(net_key),
            identity_key=crypto.k1(net_key, crypto.s1(b"nkik"), b"id128\x01"),
            beacon_key=crypto.k1(net_key, crypto.s1(b"nkbk"), b"id128\x01"),
        )

    def node_identity_matches(self, service_data: bytes, address: int) -> bool:
        """Check a Mesh Proxy "Node Identity" advertisement against an address.

        A freshly provisioned node advertises this for 60s; matching it is how
        we tell which fixture a MAC-less advertisement belongs to.
        """
        if len(service_data) < 17 or service_data[0] != 0x01:
            return False
        hash_value, random_value = service_data[1:9], service_data[9:17]
        expected = crypto.aes_ecb(
            self.identity_key,
            b"\x00" * 6 + random_value + address.to_bytes(2, "big"),
        )[8:]
        return expected == hash_value

    def network_id_matches(self, service_data: bytes) -> bool:
        """Check a Mesh Proxy "Network ID" advertisement against this network."""
        return (
            len(service_data) >= 9
            and service_data[0] == 0x00
            and service_data[1:9] == self.network_id
        )


@dataclass(frozen=True)
class NetworkMessage:
    """A decoded network PDU: the transport payload plus its routing header."""

    ctl: int
    ttl: int
    seq: int
    src: int
    dst: int
    transport_pdu: bytes


def encode_network_pdu(
    keys: NetworkKeys,
    *,
    iv_index: int,
    ctl: int,
    ttl: int,
    seq: int,
    src: int,
    dst: int,
    transport_pdu: bytes,
    proxy_config: bool = False,
) -> bytes:
    """Encrypt and obfuscate one network PDU."""
    if ctl not in (0, 1):
        raise ValueError("CTL must be 0 or 1")
    if not 0 <= ttl <= 0x7F:
        raise ValueError("TTL must fit in 7 bits")
    if not 0 <= seq <= 0xFFFFFF:
        raise ValueError("SEQ must fit in 24 bits")
    if not 0x0001 <= src <= 0x7FFF:
        raise ValueError("SRC must be a unicast address")
    if proxy_config:
        if dst != UNASSIGNED_ADDRESS:
            raise ValueError("proxy configuration DST must be unassigned")
    elif dst == UNASSIGNED_ADDRESS:
        raise ValueError("network DST must not be unassigned")
    max_transport = 12 if ctl else 16
    if not 1 <= len(transport_pdu) <= max_transport:
        raise ValueError(
            f"transport PDU must contain 1-{max_transport} bytes when CTL={ctl}"
        )

    mic_len = 8 if ctl else 4
    nonce = (
        crypto.proxy_nonce(seq, src, iv_index)
        if proxy_config
        else crypto.network_nonce(ctl, ttl, seq, src, iv_index)
    )
    encrypted = crypto.aes_ccm_encrypt(
        keys.encryption_key, nonce, dst.to_bytes(2, "big") + transport_pdu, mic_len
    )

    header = (
        bytes([(ctl << 7) | (ttl & 0x7F)])
        + seq.to_bytes(3, "big")
        + src.to_bytes(2, "big")
    )
    pecb = crypto.obfuscation_ecb(keys.privacy_key, iv_index, encrypted[:7])
    obfuscated = bytes(a ^ b for a, b in zip(header, pecb, strict=True))

    return bytes([((iv_index & 1) << 7) | (keys.nid & 0x7F)]) + obfuscated + encrypted


def decode_network_pdu(
    keys: NetworkKeys, iv_index: int, pdu: bytes, *, proxy_config: bool = False
) -> NetworkMessage:
    """Reverse :func:`encode_network_pdu`; raises :class:`NetworkDecodeError`."""
    if not 14 <= len(pdu) <= 29:
        raise NetworkDecodeError(f"invalid network PDU length {len(pdu)}")
    if pdu[0] >> 7 != iv_index & 1:
        raise NetworkDecodeError("IVI does not match the selected IV Index")
    if pdu[0] & 0x7F != keys.nid:
        raise NetworkDecodeError("NID does not match this network")

    obfuscated, encrypted = pdu[1:7], pdu[7:]
    pecb = crypto.obfuscation_ecb(keys.privacy_key, iv_index, encrypted[:7])
    header = bytes(a ^ b for a, b in zip(obfuscated, pecb, strict=True))

    ctl = header[0] >> 7
    ttl = header[0] & 0x7F
    seq = int.from_bytes(header[1:4], "big")
    src = int.from_bytes(header[4:6], "big")
    if not 0x0001 <= src <= 0x7FFF:
        raise NetworkDecodeError(f"network SRC is not unicast: {src:#06x}")
    if ctl and len(pdu) < 18:
        raise NetworkDecodeError("control network PDU is too short")

    mic_len = 8 if ctl else 4
    nonce = (
        crypto.proxy_nonce(seq, src, iv_index)
        if proxy_config
        else crypto.network_nonce(ctl, ttl, seq, src, iv_index)
    )
    try:
        plaintext = crypto.aes_ccm_decrypt(
            keys.encryption_key, nonce, encrypted, mic_len
        )
    except InvalidTag as err:
        raise NetworkDecodeError("network MIC check failed") from err

    if len(plaintext) < 3:
        raise NetworkDecodeError("network transport PDU is empty")
    dst = int.from_bytes(plaintext[:2], "big")
    if proxy_config:
        if dst != UNASSIGNED_ADDRESS:
            raise NetworkDecodeError("proxy configuration DST is not unassigned")
    elif dst == UNASSIGNED_ADDRESS:
        raise NetworkDecodeError("network DST is unassigned")

    return NetworkMessage(
        ctl=ctl,
        ttl=ttl,
        seq=seq,
        src=src,
        dst=dst,
        transport_pdu=plaintext[2:],
    )


# ─── Upper transport (access messages) ───────────────────────────────────────


def encrypt_access_payload(
    key: bytes,
    *,
    device_key: bool,
    iv_index: int,
    seq: int,
    src: int,
    dst: int,
    access_pdu: bytes,
    szmic: int = 0,
) -> bytes:
    """Encrypt an access PDU into an upper transport PDU."""
    nonce = crypto.transport_nonce(
        crypto.NONCE_DEVICE if device_key else crypto.NONCE_APPLICATION,
        szmic,
        seq,
        src,
        dst,
        iv_index,
    )
    return crypto.aes_ccm_encrypt(key, nonce, access_pdu, 8 if szmic else 4)


def decrypt_access_payload(
    key: bytes,
    *,
    device_key: bool,
    iv_index: int,
    seq: int,
    src: int,
    dst: int,
    upper_pdu: bytes,
    szmic: int = 0,
) -> bytes:
    nonce = crypto.transport_nonce(
        crypto.NONCE_DEVICE if device_key else crypto.NONCE_APPLICATION,
        szmic,
        seq,
        src,
        dst,
        iv_index,
    )
    try:
        return crypto.aes_ccm_decrypt(key, nonce, upper_pdu, 8 if szmic else 4)
    except InvalidTag as err:
        raise NetworkDecodeError("transport MIC check failed") from err


# ─── Lower transport ─────────────────────────────────────────────────────────


def build_unsegmented_access(akf: int, aid: int, upper_pdu: bytes) -> bytes:
    if not 1 <= len(upper_pdu) <= 15:
        raise ValueError("unsegmented Upper Transport PDU must contain 1-15 bytes")
    return bytes([(akf << 6) | (aid & 0x3F)]) + upper_pdu


def build_access_segments(
    akf: int, aid: int, seq_zero: int, upper_pdu: bytes, szmic: int = 0
) -> list[bytes]:
    """Split an upper transport PDU into segmented lower transport PDUs."""
    if not upper_pdu:
        raise ValueError("segmented Upper Transport PDU must not be empty")
    chunks = [
        upper_pdu[i : i + SEGMENT_PAYLOAD_SIZE]
        for i in range(0, len(upper_pdu), SEGMENT_PAYLOAD_SIZE)
    ]
    seg_n = len(chunks) - 1
    if seg_n > 31:
        raise ValueError("access message exceeds 32 segments")
    segments = []
    for seg_o, chunk in enumerate(chunks):
        # SZMIC(1) | SeqZero(13) | SegO(5) | SegN(5), packed big-endian.
        field = (
            ((szmic & 1) << 23)
            | ((seq_zero & 0x1FFF) << 10)
            | ((seg_o & 0x1F) << 5)
            | (seg_n & 0x1F)
        )
        segments.append(
            bytes([0x80 | (akf << 6) | (aid & 0x3F)]) + field.to_bytes(3, "big") + chunk
        )
    return segments


@dataclass(frozen=True)
class SegmentAck:
    seq_zero: int
    block_ack: int
    obo: bool

    def acknowledges_all(self, segment_count: int) -> bool:
        return self.block_ack == (1 << segment_count) - 1


def parse_segment_ack(params: bytes) -> SegmentAck:
    if len(params) != 6:
        raise NetworkDecodeError(f"invalid segment ack length {len(params)}")
    field = int.from_bytes(params[:2], "big")
    if field & 0x03:
        raise NetworkDecodeError(f"segment ack has non-zero RFU bits: {params.hex()}")
    return SegmentAck(
        obo=bool(field >> 15),
        seq_zero=(field >> 2) & 0x1FFF,
        block_ack=int.from_bytes(params[2:6], "big"),
    )


@dataclass(frozen=True)
class AccessSegment:
    """Validated fields from one segmented Lower Transport Access PDU."""

    akf: int
    aid: int
    szmic: int
    seq_zero: int
    seg_o: int
    seg_n: int
    seq_auth: int
    payload: bytes


@dataclass(frozen=True)
class SegmentAddResult:
    """Result of adding one segment to a reassembly transaction."""

    segment: AccessSegment
    block_ack: int
    upper_pdu: bytes | None = None
    last_segment_seq: int | None = None
    already_complete: bool = False


@dataclass
class _PendingSegments:
    akf: int
    aid: int
    szmic: int
    seg_n: int
    segments: dict[int, bytes]
    segment_sequences: dict[int, int]
    updated_at: float


@dataclass(frozen=True)
class _CompletedSegments:
    """Replay/acknowledgment state for the latest completed transaction."""

    seq_auth: int
    acked_segments: int
    akf: int
    aid: int
    szmic: int
    seg_n: int


def parse_access_segment(seq: int, lower_pdu: bytes) -> AccessSegment:
    """Validate and unpack a segmented Lower Transport Access PDU."""
    if not 0 <= seq <= 0xFFFFFF:
        raise NetworkDecodeError(f"invalid segment sequence number {seq}")
    if len(lower_pdu) < 5:
        raise NetworkDecodeError(f"short access segment: {lower_pdu.hex()}")
    if not lower_pdu[0] & 0x80:
        raise NetworkDecodeError("lower transport PDU is not segmented")

    akf = (lower_pdu[0] >> 6) & 1
    aid = lower_pdu[0] & 0x3F
    field = int.from_bytes(lower_pdu[1:4], "big")
    szmic = (field >> 23) & 1
    seq_zero = (field >> 10) & 0x1FFF
    seg_o = (field >> 5) & 0x1F
    seg_n = field & 0x1F
    payload = lower_pdu[4:]

    if seg_o > seg_n:
        raise NetworkDecodeError(f"segment offset {seg_o} exceeds last segment {seg_n}")
    if len(payload) > SEGMENT_PAYLOAD_SIZE:
        raise NetworkDecodeError(f"access segment payload is too long: {len(payload)}")
    if seg_o < seg_n and len(payload) != SEGMENT_PAYLOAD_SIZE:
        raise NetworkDecodeError(
            f"non-final access segment has {len(payload)} bytes, expected 12"
        )

    # SeqAuth is the greatest sequence number no larger than SEQ whose low
    # 13 bits equal SeqZero. A negative result cannot belong to this IV Index.
    seq_auth = seq - ((seq - seq_zero) & 0x1FFF)
    if seq_auth < 0:
        raise NetworkDecodeError(
            f"segment SeqZero {seq_zero:#06x} is impossible for sequence {seq:#08x}"
        )

    return AccessSegment(
        akf=akf,
        aid=aid,
        szmic=szmic,
        seq_zero=seq_zero,
        seg_o=seg_o,
        seg_n=seg_n,
        seq_auth=seq_auth,
        payload=payload,
    )


class SegmentReassembler:
    """Bounded, expiring reassembly keyed by source, destination and SeqAuth."""

    def __init__(
        self,
        *,
        timeout: float = SEGMENT_REASSEMBLY_TIMEOUT,
        max_pending: int = MAX_PENDING_REASSEMBLIES,
        max_completed: int = MAX_COMPLETED_REASSEMBLIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if timeout <= 0:
            raise ValueError("reassembly timeout must be positive")
        if max_pending < 1 or max_completed < 1:
            raise ValueError("reassembly limits must be positive")
        self._timeout = timeout
        self._max_pending = max_pending
        self._max_completed = max_completed
        self._clock = clock
        self._pending: dict[tuple[int, int, int], _PendingSegments] = {}
        # Mesh Protocol 3.5.3.4 requires the most recent completed SeqAuth and
        # AckedSegments pair to remain available for repeated segments.  These
        # records therefore do not expire.  Capacity is fixed: evicting one
        # would turn a delayed replay into a newly deliverable access message.
        self._completed: dict[tuple[int, int], _CompletedSegments] = {}
        # Expiring the SAR payload does not make its SeqAuth reusable. Retain
        # a watermark for discarded transactions so delayed fragments cannot
        # resurrect them; only a greater SeqAuth may start a new transaction.
        self._discarded: dict[tuple[int, int], int] = {}

    def _prune(self, now: float) -> None:
        cutoff = now - self._timeout
        for key, pending in list(self._pending.items()):
            if pending.updated_at <= cutoff:
                del self._pending[key]
                src, dst, seq_auth = key
                pair = (src, dst)
                self._discarded[pair] = max(seq_auth, self._discarded.get(pair, -1))
                completed = self._completed.get(pair)
                if completed is not None and completed.seq_auth <= seq_auth:
                    del self._completed[pair]

    @staticmethod
    def _bitmap(segments: dict[int, bytes]) -> int:
        bitmap = 0
        for seg_o in segments:
            bitmap |= 1 << seg_o
        return bitmap

    def completed_ack(
        self, src: int, dst: int, seq_auth: int, *, now: float | None = None
    ) -> int | None:
        """Return the stored AckedSegments for the latest completed SeqAuth."""
        current = self._clock() if now is None else now
        self._prune(current)
        if self._discarded.get((src, dst), -1) >= seq_auth:
            return None
        completed = self._completed.get((src, dst))
        if completed is None or completed.seq_auth != seq_auth:
            return None
        return completed.acked_segments

    def add(
        self,
        src: int,
        dst: int,
        seq: int,
        lower_pdu: bytes,
        *,
        parsed: AccessSegment | None = None,
        now: float | None = None,
    ) -> SegmentAddResult:
        """Feed one segment.

        The result includes the current block-ack bitmap and, once complete,
        the reassembled Upper Transport PDU and the SEQ of its final segment.
        """
        segment = parsed or parse_access_segment(seq, lower_pdu)
        current = self._clock() if now is None else now
        self._prune(current)
        key = (src, dst, segment.seq_auth)
        pair = (src, dst)

        if segment.seq_auth <= self._discarded.get(pair, -1):
            raise NetworkDecodeError("late segment from discarded transaction")

        completed = self._completed.get(pair)
        if completed is not None:
            if segment.seq_auth < completed.seq_auth:
                raise NetworkDecodeError("stale segmented transaction")
            if segment.seq_auth == completed.seq_auth:
                if (
                    segment.akf != completed.akf
                    or segment.aid != completed.aid
                    or segment.szmic != completed.szmic
                    or segment.seg_n != completed.seg_n
                ):
                    raise NetworkDecodeError("completed transaction metadata changed")
                return SegmentAddResult(
                    segment=segment,
                    block_ack=completed.acked_segments,
                    already_complete=True,
                )

        pending = self._pending.get(key)
        if pending is None:
            # A sender shall not have two segmented transactions to the same
            # destination in flight. A newer SeqAuth supersedes an abandoned
            # transaction; an older one is stale and must not replace it.
            for old_key in list(self._pending):
                old_src, old_dst, old_seq_auth = old_key
                if old_src != src or old_dst != dst:
                    continue
                if old_seq_auth > segment.seq_auth:
                    raise NetworkDecodeError("stale segmented transaction")
                del self._pending[old_key]

            if len(self._pending) >= self._max_pending:
                raise NetworkDecodeError("segment reassembly capacity exhausted")
            tracked_pairs = set(self._completed)
            tracked_pairs.update(self._discarded)
            tracked_pairs.update((item[0], item[1]) for item in self._pending)
            if pair not in tracked_pairs and len(tracked_pairs) >= self._max_completed:
                raise NetworkDecodeError(
                    "completed transaction tracking capacity exhausted"
                )
            pending = _PendingSegments(
                akf=segment.akf,
                aid=segment.aid,
                szmic=segment.szmic,
                seg_n=segment.seg_n,
                segments={},
                segment_sequences={},
                updated_at=current,
            )
            self._pending[key] = pending
        elif (
            pending.akf != segment.akf
            or pending.aid != segment.aid
            or pending.szmic != segment.szmic
            or pending.seg_n != segment.seg_n
        ):
            raise NetworkDecodeError("inconsistent segmented transaction metadata")

        old_payload = pending.segments.get(segment.seg_o)
        if old_payload is not None and old_payload != segment.payload:
            raise NetworkDecodeError("retransmitted segment payload changed")
        pending.segments[segment.seg_o] = segment.payload
        pending.segment_sequences[segment.seg_o] = max(
            seq, pending.segment_sequences.get(segment.seg_o, seq)
        )
        pending.updated_at = current
        block_ack = self._bitmap(pending.segments)
        full_ack = (1 << (segment.seg_n + 1)) - 1
        if block_ack != full_ack:
            return SegmentAddResult(segment=segment, block_ack=block_ack)

        upper = b"".join(pending.segments[i] for i in range(segment.seg_n + 1))
        last_segment_seq = pending.segment_sequences[segment.seg_n]
        del self._pending[key]
        self._discarded.pop(pair, None)
        self._completed[pair] = _CompletedSegments(
            seq_auth=segment.seq_auth,
            acked_segments=full_ack,
            akf=segment.akf,
            aid=segment.aid,
            szmic=segment.szmic,
            seg_n=segment.seg_n,
        )
        return SegmentAddResult(
            segment=segment,
            block_ack=full_ack,
            upper_pdu=upper,
            last_segment_seq=last_segment_seq,
        )


# ─── Access layer ────────────────────────────────────────────────────────────


def encode_opcode(opcode: int) -> bytes:
    """Serialise an access opcode (1, 2 or 3 octets) per section 3.7.3.1."""
    if opcode < 0x80:
        return bytes([opcode])
    if opcode < 0x10000:
        return opcode.to_bytes(2, "big")
    return opcode.to_bytes(3, "big")


def decode_opcode(access_pdu: bytes) -> tuple[int, bytes]:
    """Split an access PDU into ``(opcode, parameters)``."""
    if not access_pdu:
        raise NetworkDecodeError("empty access PDU")
    first = access_pdu[0]
    if first & 0x80 == 0:
        return first, access_pdu[1:]
    if first & 0xC0 == 0x80:
        if len(access_pdu) < 2:
            raise NetworkDecodeError("truncated 2-octet opcode")
        return int.from_bytes(access_pdu[:2], "big"), access_pdu[2:]
    if len(access_pdu) < 3:
        raise NetworkDecodeError("truncated 3-octet opcode")
    return int.from_bytes(access_pdu[:3], "big"), access_pdu[3:]
