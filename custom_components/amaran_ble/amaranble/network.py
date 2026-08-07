"""Mesh network and transport layer framing (Mesh Profile 1.0.1, sections 3.4-3.6).

Pure encode/decode of the layers between an access message and the bytes that
go into a Proxy PDU. No I/O, so it can be exercised without a fixture present.
"""

from __future__ import annotations

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
    if len(pdu) < 14:
        raise NetworkDecodeError(f"network PDU too short: {pdu.hex()}")
    if pdu[0] & 0x7F != keys.nid:
        raise NetworkDecodeError("NID does not match this network")

    obfuscated, encrypted = pdu[1:7], pdu[7:]
    pecb = crypto.obfuscation_ecb(keys.privacy_key, iv_index, encrypted[:7])
    header = bytes(a ^ b for a, b in zip(obfuscated, pecb, strict=True))

    ctl = header[0] >> 7
    ttl = header[0] & 0x7F
    seq = int.from_bytes(header[1:4], "big")
    src = int.from_bytes(header[4:6], "big")

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

    return NetworkMessage(
        ctl=ctl,
        ttl=ttl,
        seq=seq,
        src=src,
        dst=int.from_bytes(plaintext[:2], "big"),
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
    return bytes([(akf << 6) | (aid & 0x3F)]) + upper_pdu


def build_access_segments(
    akf: int, aid: int, seq_zero: int, upper_pdu: bytes, szmic: int = 0
) -> list[bytes]:
    """Split an upper transport PDU into segmented lower transport PDUs."""
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
    if len(params) < 6:
        raise NetworkDecodeError(f"short segment ack: {params.hex()}")
    field = int.from_bytes(params[:2], "big")
    return SegmentAck(
        obo=bool(field >> 15),
        seq_zero=(field >> 2) & 0x1FFF,
        block_ack=int.from_bytes(params[2:6], "big"),
    )


class SegmentReassembler:
    """Collects incoming segmented access messages keyed by (src, seq_zero)."""

    def __init__(self) -> None:
        self._pending: dict[tuple[int, int], dict[int, bytes]] = {}
        self._meta: dict[tuple[int, int], tuple[int, int, int, int]] = {}

    def add(
        self, src: int, seq: int, lower_pdu: bytes
    ) -> tuple[bytes, int, int, int, int] | None:
        """Feed one segment.

        Returns ``(upper_pdu, akf, aid, szmic, seq_auth)`` once the message is
        complete, otherwise ``None``.
        """
        akf = (lower_pdu[0] >> 6) & 1
        aid = lower_pdu[0] & 0x3F
        field = int.from_bytes(lower_pdu[1:4], "big")
        szmic = (field >> 23) & 1
        seq_zero = (field >> 10) & 0x1FFF
        seg_o = (field >> 5) & 0x1F
        seg_n = field & 0x1F

        key = (src, seq_zero)
        self._pending.setdefault(key, {})[seg_o] = lower_pdu[4:]
        # SeqAuth is the sequence number the first segment was sent with; it is
        # what the upper transport nonce must use, not this segment's own seq.
        seq_auth = seq - ((seq - seq_zero) & 0x1FFF)
        self._meta[key] = (akf, aid, szmic, seq_auth)

        segments = self._pending[key]
        if len(segments) <= seg_n:
            return None
        upper = b"".join(segments[i] for i in range(seg_n + 1))
        del self._pending[key]
        del self._meta[key]
        return upper, akf, aid, szmic, seq_auth

    def block_ack(self, src: int, seq_zero: int) -> int:
        acc = 0
        for seg_o in self._pending.get((src, seq_zero), {}):
            acc |= 1 << seg_o
        return acc


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
