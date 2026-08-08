"""PB-GATT provisioning transaction tests."""

from __future__ import annotations

import pytest
from amaranble import crypto, provisioning
from amaranble.gatt import TYPE_PROVISIONING
from amaranble.provisioning import (
    CAPABILITIES,
    COMPLETE,
    CONFIRMATION,
    DATA,
    PUBLIC_KEY,
    RANDOM,
    Provisioner,
    ProvisioningError,
)


class NoopClient:
    pass


class RecordingTransport:
    def __init__(self, events: list[object]) -> None:
        self.events = events

    async def start(self) -> None:
        self.events.append("start")

    async def send(self, _msg_type: int, payload: bytes) -> None:
        self.events.append(("send", payload[0]))

    async def stop(self) -> None:
        self.events.append("stop")


class FakeKeyPair:
    public_key_bytes = b"P" * 64

    def shared_secret(self, device_public_key: bytes) -> bytes:
        assert device_public_key == b"D" * 64
        return b"S" * 32


def _prepared_provisioner(
    monkeypatch,
    events: list[object],
    *,
    device_public_key: bytes = b"D" * 64,
    device_confirmation: bytes = b"D" * 16,
    device_random: bytes = b"Q" * 16,
    complete_params: bytes = b"",
) -> Provisioner:
    """Build a deterministic successful exchange, up to the commit hook."""
    device_key = b"K" * 16

    monkeypatch.setattr(crypto, "ProvisioningKeyPair", FakeKeyPair)
    monkeypatch.setattr(crypto, "random_bytes", lambda _length: b"R" * 16)
    monkeypatch.setattr(crypto, "s1", lambda _data: b"S" * 16)
    monkeypatch.setattr(
        crypto,
        "k1",
        lambda _key, _salt, info: {
            b"prck": b"C" * 16,
            b"prsk": b"E" * 16,
            b"prsn": b"N" * 16,
            b"prdk": device_key,
        }[info],
    )
    monkeypatch.setattr(
        crypto,
        "aes_cmac",
        lambda _key, message: b"P" * 16 if message.startswith(b"R" * 16) else b"D" * 16,
    )
    monkeypatch.setattr(
        crypto,
        "aes_ccm_encrypt",
        lambda _key, _nonce, _data, _mic_len: b"encrypted provisioning data",
    )

    provisioner = Provisioner(NoopClient())
    provisioner._transport = RecordingTransport(events)  # type: ignore[assignment]
    provisioner._queue.put_nowait(
        (CAPABILITIES, bytes.fromhex("0100010000000000000000"))
    )
    provisioner._queue.put_nowait((PUBLIC_KEY, device_public_key))
    provisioner._queue.put_nowait((CONFIRMATION, device_confirmation))
    provisioner._queue.put_nowait((RANDOM, device_random))
    provisioner._queue.put_nowait((COMPLETE, complete_params))
    return provisioner


@pytest.mark.asyncio
async def test_before_commit_runs_before_provisioning_data(monkeypatch) -> None:
    events: list[object] = []
    provisioner = _prepared_provisioner(monkeypatch, events)

    async def before_commit(device_key: bytes, num_elements: int) -> None:
        events.append(("before_commit", device_key, num_elements))

    result = await provisioner.provision(
        b"N" * 16,
        0x0002,
        before_commit=before_commit,
    )

    commit_event = ("before_commit", b"K" * 16, 1)
    assert commit_event in events
    assert events.index(commit_event) < events.index(("send", DATA))
    assert events[-1] == "stop"
    assert result.device_key == b"K" * 16


@pytest.mark.asyncio
async def test_before_commit_failure_aborts_before_provisioning_data(
    monkeypatch,
) -> None:
    events: list[object] = []
    provisioner = _prepared_provisioner(monkeypatch, events)

    async def before_commit(_device_key: bytes, _num_elements: int) -> None:
        events.append("commit refused")
        raise OSError("credentials were not durable")

    with pytest.raises(OSError, match="not durable"):
        await provisioner.provision(
            b"N" * 16,
            0x0002,
            before_commit=before_commit,
        )

    assert "commit refused" in events
    assert ("send", DATA) not in events
    assert provisioner._queue.qsize() == 1
    assert events[-1] == "stop"


@pytest.mark.asyncio
async def test_unexpected_pdu_fails_immediately() -> None:
    provisioner = Provisioner(NoopClient())
    provisioner._queue.put_nowait((PUBLIC_KEY, b"unexpected"))

    with pytest.raises(ProvisioningError, match="unexpected provisioning PDU"):
        await provisioner._expect(CAPABILITIES, timeout=1)


@pytest.mark.asyncio
async def test_reserved_provisioning_type_bits_are_not_masked() -> None:
    """An RFU PDU type cannot impersonate an assigned provisioning PDU."""
    provisioner = Provisioner(NoopClient())
    provisioner._on_message(
        TYPE_PROVISIONING,
        bytes([0x40 | CAPABILITIES]) + bytes(11),
    )

    with pytest.raises(ProvisioningError, match=r"unexpected provisioning PDU 0x41"):
        await provisioner._expect(CAPABILITIES)


@pytest.mark.asyncio
async def test_reflected_public_key_is_rejected_and_transport_stops(
    monkeypatch,
) -> None:
    events: list[object] = []
    provisioner = _prepared_provisioner(
        monkeypatch, events, device_public_key=FakeKeyPair.public_key_bytes
    )

    with pytest.raises(ProvisioningError, match=r"reflected.*public key"):
        await provisioner.provision(b"N" * 16, 2)
    assert events[-1] == "stop"


@pytest.mark.asyncio
async def test_invalid_public_key_is_a_typed_error(monkeypatch) -> None:
    events: list[object] = []
    provisioner = _prepared_provisioner(monkeypatch, events)

    class InvalidKeyPair(FakeKeyPair):
        def shared_secret(self, _device_public_key: bytes) -> bytes:
            raise ValueError("point is not on curve")

    monkeypatch.setattr(crypto, "ProvisioningKeyPair", InvalidKeyPair)
    with pytest.raises(ProvisioningError, match="invalid P-256 public key"):
        await provisioner.provision(b"N" * 16, 2)
    assert events[-1] == "stop"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("device_confirmation", "match"),
    [(b"P" * 16, "reflected.*confirmation"), (b"short", "must be 16 bytes")],
)
async def test_invalid_confirmation_is_rejected(
    monkeypatch, device_confirmation: bytes, match: str
) -> None:
    events: list[object] = []
    provisioner = _prepared_provisioner(
        monkeypatch, events, device_confirmation=device_confirmation
    )

    with pytest.raises(ProvisioningError, match=match):
        await provisioner.provision(b"N" * 16, 2)
    assert ("send", RANDOM) not in events
    assert events[-1] == "stop"


@pytest.mark.asyncio
async def test_invalid_random_and_complete_are_rejected(monkeypatch) -> None:
    events: list[object] = []
    provisioner = _prepared_provisioner(monkeypatch, events, device_random=b"short")
    with pytest.raises(ProvisioningError, match="random must be 16 bytes"):
        await provisioner.provision(b"N" * 16, 2)
    assert ("send", DATA) not in events
    assert events[-1] == "stop"

    events = []
    provisioner = _prepared_provisioner(
        monkeypatch, events, complete_params=b"not empty"
    )
    with pytest.raises(ProvisioningError, match="Complete PDU must be empty"):
        await provisioner.provision(b"N" * 16, 2)
    assert events[-1] == "stop"


@pytest.mark.asyncio
async def test_transaction_timeout_is_typed_and_cleans_up(monkeypatch) -> None:
    events: list[object] = []
    monkeypatch.setattr(provisioning, "PROVISIONING_TRANSACTION_TIMEOUT", 0.01)
    provisioner = Provisioner(NoopClient())
    provisioner._transport = RecordingTransport(events)  # type: ignore[assignment]

    with pytest.raises(ProvisioningError, match="transaction timed out"):
        await provisioner.provision(b"N" * 16, 2)
    assert events[-1] == "stop"


@pytest.mark.asyncio
async def test_cleanup_failure_does_not_mask_provisioning_error() -> None:
    class FailingTransport(RecordingTransport):
        async def stop(self) -> None:
            self.events.append("stop")
            raise OSError("teardown failed")

    events: list[object] = []
    provisioner = Provisioner(NoopClient())
    provisioner._transport = FailingTransport(events)  # type: ignore[assignment]
    provisioner._queue.put_nowait((PUBLIC_KEY, b"unexpected"))

    with pytest.raises(ProvisioningError, match="unexpected provisioning PDU"):
        await provisioner.provision(b"N" * 16, 2)
    assert events[-1] == "stop"
