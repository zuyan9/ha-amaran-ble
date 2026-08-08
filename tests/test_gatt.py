"""Proxy-PDU GATT segmentation tests."""

from __future__ import annotations

import asyncio

import pytest
from amaranble import gatt
from amaranble.gatt import (
    SAR_CONTINUATION,
    SAR_FIRST,
    SAR_LAST,
    TYPE_NETWORK,
    MeshGattTransport,
)


class FakeClient:
    def __init__(self) -> None:
        self.writes: list[tuple[str, bytes, bool]] = []
        self.notify_callback = None
        self.stopped: list[str] = []

    async def start_notify(self, char: str, callback) -> None:
        self.notify_callback = callback

    async def stop_notify(self, char: str) -> None:
        self.stopped.append(char)

    async def write_gatt_char(self, char: str, data: bytes, *, response: bool) -> None:
        self.writes.append((char, data, response))


@pytest.mark.asyncio
async def test_outbound_segmentation() -> None:
    client = FakeClient()
    transport = MeshGattTransport(client, "in", "out", lambda *_: None)
    payload = bytes(range(40))

    await transport.send(TYPE_NETWORK, payload)

    assert [write[1][0] for write in client.writes] == [
        (SAR_FIRST << 6) | TYPE_NETWORK,
        (SAR_CONTINUATION << 6) | TYPE_NETWORK,
        (SAR_LAST << 6) | TYPE_NETWORK,
    ]
    assert b"".join(write[1][1:] for write in client.writes) == payload
    assert all(write[2] is False for write in client.writes)


@pytest.mark.asyncio
async def test_inbound_reassembly_and_orphan_drop() -> None:
    received: list[tuple[int, bytes]] = []
    client = FakeClient()
    transport = MeshGattTransport(
        client, "in", "out", lambda msg_type, body: received.append((msg_type, body))
    )
    await transport.start()
    callback = client.notify_callback
    assert callback is not None

    callback(None, bytearray([(SAR_CONTINUATION << 6) | TYPE_NETWORK, 99]))
    callback(None, bytearray([(SAR_FIRST << 6) | TYPE_NETWORK, 1, 2]))
    callback(None, bytearray([(SAR_CONTINUATION << 6) | TYPE_NETWORK, 3]))
    callback(None, bytearray([(SAR_LAST << 6) | TYPE_NETWORK, 4, 5]))

    assert received == [(TYPE_NETWORK, b"\x01\x02\x03\x04\x05")]
    await transport.stop()
    assert client.stopped == ["out"]


@pytest.mark.asyncio
async def test_async_notification_handler_is_scheduled() -> None:
    event = asyncio.Event()

    async def handler(_msg_type: int, _payload: bytes) -> None:
        event.set()

    client = FakeClient()
    transport = MeshGattTransport(client, "in", "out", handler)
    await transport.start()
    client.notify_callback(None, bytearray([TYPE_NETWORK, 1]))
    await asyncio.wait_for(event.wait(), timeout=1)


@pytest.mark.asyncio
async def test_write_timeout_prevents_stuck_proxy_setup(monkeypatch) -> None:
    class HangingClient(FakeClient):
        async def write_gatt_char(
            self, char: str, data: bytes, *, response: bool
        ) -> None:
            await asyncio.Event().wait()

    monkeypatch.setattr(gatt, "GATT_OPERATION_TIMEOUT", 0.01)
    transport = MeshGattTransport(HangingClient(), "in", "out", lambda *_: None)

    with pytest.raises(TimeoutError):
        await transport.send(TYPE_NETWORK, b"stuck")


@pytest.mark.asyncio
async def test_notify_operations_are_bounded(monkeypatch) -> None:
    class HangingNotifyClient(FakeClient):
        async def start_notify(self, char: str, callback) -> None:
            await asyncio.Event().wait()

        async def stop_notify(self, char: str) -> None:
            await asyncio.Event().wait()

    monkeypatch.setattr(gatt, "GATT_OPERATION_TIMEOUT", 0.01)
    transport = MeshGattTransport(HangingNotifyClient(), "in", "out", lambda *_: None)

    with pytest.raises(TimeoutError):
        await transport.start()
    # Teardown is best effort and must return after its own timeout.
    await transport.stop()
