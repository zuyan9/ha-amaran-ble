"""Proxy-PDU GATT segmentation tests."""

from __future__ import annotations

import asyncio

import pytest
from amaranble import gatt
from amaranble.gatt import (
    SAR_COMPLETE,
    SAR_CONTINUATION,
    SAR_FIRST,
    SAR_LAST,
    TYPE_NETWORK,
    TYPE_PROVISIONING,
    MeshGattTransport,
)


class FakeClient:
    def __init__(self) -> None:
        self.writes: list[tuple[str, bytes, bool]] = []
        self.notify_callback = None
        self.stopped: list[str] = []
        self.disconnected = 0

    async def start_notify(self, char: str, callback) -> None:
        self.notify_callback = callback

    async def stop_notify(self, char: str) -> None:
        self.stopped.append(char)

    async def disconnect(self) -> None:
        self.disconnected += 1

    async def write_gatt_char(self, char: str, data: bytes, *, response: bool) -> None:
        self.writes.append((char, data, response))


@pytest.mark.asyncio
async def test_outbound_segmentation() -> None:
    client = FakeClient()
    transport = MeshGattTransport(client, "in", "out", lambda *_: None)
    payload = bytes(range(40))

    await transport.send(TYPE_PROVISIONING, payload)

    assert [write[1][0] for write in client.writes] == [
        (SAR_FIRST << 6) | TYPE_PROVISIONING,
        (SAR_CONTINUATION << 6) | TYPE_PROVISIONING,
        (SAR_LAST << 6) | TYPE_PROVISIONING,
    ]
    assert b"".join(write[1][1:] for write in client.writes) == payload
    assert all(write[2] is False for write in client.writes)


@pytest.mark.asyncio
async def test_inbound_reassembly() -> None:
    received: list[tuple[int, bytes]] = []
    client = FakeClient()
    transport = MeshGattTransport(
        client, "in", "out", lambda msg_type, body: received.append((msg_type, body))
    )
    await transport.start()
    callback = client.notify_callback
    assert callback is not None

    callback(None, bytearray([(SAR_FIRST << 6) | TYPE_NETWORK, 1, 2]))
    callback(None, bytearray([(SAR_CONTINUATION << 6) | TYPE_NETWORK, 3]))
    callback(None, bytearray([(SAR_LAST << 6) | TYPE_NETWORK, 4, 5]))

    assert received == [(TYPE_NETWORK, b"\x01\x02\x03\x04\x05")]
    await transport.stop()
    assert client.stopped == ["out"]


@pytest.mark.asyncio
async def test_invalid_sar_disconnects_and_cannot_splice_stale_data() -> None:
    received: list[tuple[int, bytes]] = []
    client = FakeClient()
    transport = MeshGattTransport(
        client, "in", "out", lambda msg_type, body: received.append((msg_type, body))
    )
    await transport.start()
    callback = client.notify_callback
    assert callback is not None

    callback(None, bytearray([(SAR_FIRST << 6) | TYPE_NETWORK]) + b"A")
    callback(None, bytearray([(SAR_COMPLETE << 6) | TYPE_NETWORK]) + b"X")
    callback(None, bytearray([(SAR_LAST << 6) | TYPE_NETWORK]) + b"B")
    await asyncio.sleep(0)

    assert received == []
    assert client.disconnected == 1
    assert transport._rx_type is None
    assert transport._rx_buf == b""


@pytest.mark.asyncio
async def test_orphan_sar_fragment_disconnects() -> None:
    client = FakeClient()
    transport = MeshGattTransport(client, "in", "out", lambda *_: None)
    await transport.start()

    client.notify_callback(
        None, bytearray([(SAR_CONTINUATION << 6) | TYPE_NETWORK, 99])
    )
    await asyncio.sleep(0)

    assert client.disconnected == 1


@pytest.mark.asyncio
async def test_sar_timeout_disconnects(monkeypatch) -> None:
    monkeypatch.setattr(gatt, "PROXY_SAR_TIMEOUT", 0.01)
    client = FakeClient()
    transport = MeshGattTransport(client, "in", "out", lambda *_: None)
    await transport.start()

    client.notify_callback(None, bytearray([(SAR_FIRST << 6) | TYPE_NETWORK, 1]))
    await asyncio.sleep(0.02)

    assert client.disconnected == 1
    assert transport._rx_type is None


@pytest.mark.asyncio
async def test_proxy_message_lengths_are_bounded() -> None:
    client = FakeClient()
    transport = MeshGattTransport(client, "in", "out", lambda *_: None)
    with pytest.raises(ValueError, match="29-byte limit"):
        await transport.send(TYPE_NETWORK, bytes(30))

    await transport.start()
    client.notify_callback(None, bytearray([TYPE_NETWORK]) + bytes(30))
    await asyncio.sleep(0)
    assert client.disconnected == 1


@pytest.mark.asyncio
async def test_mesh_11_private_beacon_length_is_accepted() -> None:
    """A 27-byte Mesh Private beacon is valid on the GATT bearer."""
    client = FakeClient()
    received: list[tuple[int, bytes]] = []
    transport = MeshGattTransport(
        client,
        "in",
        "out",
        lambda msg_type, body: received.append((msg_type, body)),
    )

    private_beacon = b"\x02" + b"r" * 13 + b"o" * 5 + b"a" * 8
    transport._notification_handler(
        None, bytearray([gatt.TYPE_MESH_BEACON]) + private_beacon
    )

    assert received == [(gatt.TYPE_MESH_BEACON, private_beacon)]


@pytest.mark.asyncio
async def test_oversized_mesh_beacon_is_rejected() -> None:
    """The Mesh 1.1 beacon bound still prevents unbounded input."""
    client = FakeClient()
    transport = MeshGattTransport(client, "in", "out", lambda *_: None)

    transport._notification_handler(
        None, bytearray([gatt.TYPE_MESH_BEACON]) + bytearray(28)
    )
    await asyncio.sleep(0)

    assert client.disconnected == 1


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


@pytest.mark.asyncio
async def test_protocol_failure_disconnect_is_bounded_and_suppressed(
    monkeypatch,
) -> None:
    class HangingDisconnectClient(FakeClient):
        async def disconnect(self) -> None:
            await asyncio.Event().wait()

    monkeypatch.setattr(gatt, "GATT_OPERATION_TIMEOUT", 0.01)
    client = HangingDisconnectClient()
    transport = MeshGattTransport(client, "in", "out", lambda *_: None)
    await transport.start()

    client.notify_callback(None, bytearray([(SAR_LAST << 6) | TYPE_NETWORK]))
    await asyncio.sleep(0.02)

    assert transport._protocol_failed
    assert transport._background_tasks == set()
