"""PB-GATT provisioning transaction tests."""

from __future__ import annotations

import pytest
from amaranble import crypto
from amaranble.provisioning import (
    CAPABILITIES,
    COMPLETE,
    CONFIRMATION,
    DATA,
    PUBLIC_KEY,
    RANDOM,
    Provisioner,
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


def _prepared_provisioner(monkeypatch, events: list[object]) -> Provisioner:
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
    monkeypatch.setattr(crypto, "aes_cmac", lambda _key, _message: b"M" * 16)
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
    provisioner._queue.put_nowait((PUBLIC_KEY, b"D" * 64))
    provisioner._queue.put_nowait((CONFIRMATION, b"M" * 16))
    provisioner._queue.put_nowait((RANDOM, b"Q" * 16))
    provisioner._queue.put_nowait((COMPLETE, b""))
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
