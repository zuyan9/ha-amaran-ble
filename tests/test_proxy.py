"""Mesh Proxy client behavior."""

from __future__ import annotations

import asyncio

import pytest
from amaranble import crypto, network
from amaranble.gatt import TYPE_NETWORK
from amaranble.proxy import (
    PROXY_CONFIG_ADD_ADDRESSES,
    PROXY_CONFIG_FILTER_STATUS,
    PROXY_CONFIG_SET_FILTER_TYPE,
    PROXY_FILTER_ACCEPT_LIST,
    AccessMessage,
    ProxyClient,
    ProxyError,
    _PendingSegmentAck,
)
from amaranble.sequence import SequenceReservation

NET_KEY = bytes.fromhex("f7a2a44f8e8a8029064f173ddc1e2b00")
APP_KEY = bytes.fromhex("3216d1509884b533248541792b877f98")


class NoopClient:
    pass


class RecordingTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[int, bytes]] = []

    async def send(self, msg_type: int, payload: bytes) -> None:
        self.sent.append((msg_type, payload))

    async def stop(self) -> None:
        return None


@pytest.mark.asyncio
async def test_proxy_filter_observes_telink_settling_delays(monkeypatch) -> None:
    proxy = ProxyClient(
        NoopClient(),
        net_key=NET_KEY,
        app_key=APP_KEY,
        device_keys={},
    )
    events: list[tuple[str, float | int]] = []

    class StartTransport:
        async def start(self) -> None:
            events.append(("start", 0))

    async def sleep(delay: float) -> None:
        events.append(("sleep", delay))

    async def send_proxy_config(opcode: int, _params: bytes) -> None:
        events.append(("send", opcode))
        assert proxy._filter_status is not None
        proxy._filter_status.set_result(None)

    proxy._transport = StartTransport()  # type: ignore[assignment]
    proxy._send_proxy_config = send_proxy_config  # type: ignore[method-assign]
    monkeypatch.setattr(asyncio, "sleep", sleep)

    await proxy.start([2])

    assert events == [
        ("start", 0),
        ("sleep", 0.5),
        ("send", PROXY_CONFIG_SET_FILTER_TYPE),
        ("send", PROXY_CONFIG_ADD_ADDRESSES),
        ("sleep", 0.3),
    ]


@pytest.mark.asyncio
async def test_proxy_start_cancellation_cleans_up_partial_transport(
    monkeypatch,
) -> None:
    proxy = ProxyClient(
        NoopClient(),
        net_key=NET_KEY,
        app_key=APP_KEY,
        device_keys={},
    )
    events: list[str] = []

    class PartialTransport:
        async def start(self) -> None:
            events.append("start")

        async def stop(self) -> None:
            events.append("stop")

    async def cancelled_sleep(_delay: float) -> None:
        raise asyncio.CancelledError

    proxy._transport = PartialTransport()  # type: ignore[assignment]
    monkeypatch.setattr(asyncio, "sleep", cancelled_sleep)

    with pytest.raises(asyncio.CancelledError):
        await proxy.start()
    assert events == ["start", "stop"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_status",
    [
        bytes.fromhex("83000000"),  # RFU opcode bit must not be masked away
        bytes.fromhex("030000"),  # missing one List Size byte
        bytes.fromhex("0300000000"),  # trailing byte
        bytes.fromhex("03010000"),  # reject-list status was not requested
    ],
)
async def test_filter_status_is_strictly_validated(invalid_status: bytes) -> None:
    proxy = ProxyClient(
        NoopClient(),
        net_key=NET_KEY,
        app_key=APP_KEY,
        device_keys={},
    )
    loop = asyncio.get_running_loop()
    proxy._filter_status = loop.create_future()

    def deliver(lower: bytes) -> None:
        wire = network.encode_network_pdu(
            proxy._keys,
            iv_index=0,
            ctl=1,
            ttl=0,
            seq=10,
            src=2,
            dst=network.UNASSIGNED_ADDRESS,
            transport_pdu=lower,
            proxy_config=True,
        )
        proxy._handle_proxy_config(wire)

    deliver(invalid_status)
    assert not proxy._filter_status.done()
    assert proxy._replay_list == {}

    deliver(
        bytes(
            [
                PROXY_CONFIG_FILTER_STATUS,
                PROXY_FILTER_ACCEPT_LIST,
                0x00,
                0x02,
            ]
        )
    )
    assert proxy._filter_status.done()
    assert proxy._replay_list == {2: 10}


@pytest.mark.asyncio
async def test_inbound_segments_are_acknowledged_and_dispatched() -> None:
    received: list[AccessMessage] = []
    proxy = ProxyClient(
        NoopClient(),
        net_key=NET_KEY,
        app_key=APP_KEY,
        device_keys={},
        local_address=1,
        sequence=100,
        on_message=received.append,
    )
    transport = RecordingTransport()
    proxy._transport = transport  # type: ignore[assignment]

    src, dst, first_seq = 2, 1, 0x2345
    access_pdu = network.encode_opcode(0x26) + bytes(range(25))
    upper = network.encrypt_access_payload(
        APP_KEY,
        device_key=False,
        iv_index=0,
        seq=first_seq,
        src=src,
        dst=dst,
        access_pdu=access_pdu,
    )
    segments = network.build_access_segments(
        1, crypto.k4(APP_KEY), first_seq & 0x1FFF, upper
    )
    keys = network.NetworkKeys.derive(NET_KEY)

    for index, lower in enumerate(segments):
        inbound = network.encode_network_pdu(
            keys,
            iv_index=0,
            ctl=0,
            ttl=5,
            seq=first_seq + index,
            src=src,
            dst=dst,
            transport_pdu=lower,
        )
        proxy._handle_network(inbound)

    await asyncio.gather(*proxy._background_tasks)

    assert received == [
        AccessMessage(
            src=2,
            dst=1,
            opcode=0x26,
            parameters=bytes(range(25)),
            device_key=False,
        )
    ]
    assert len(transport.sent) == 1
    msg_type, ack_pdu = transport.sent[0]
    assert msg_type == TYPE_NETWORK
    ack_message = network.decode_network_pdu(keys, 0, ack_pdu)
    assert ack_message.ctl == 1
    assert ack_message.src == 1
    assert ack_message.dst == 2
    assert ack_message.transport_pdu[0] == network.CONTROL_OPCODE_SEGMENT_ACK
    ack = network.parse_segment_ack(ack_message.transport_pdu[1:])
    assert ack.seq_zero == first_seq & 0x1FFF
    assert ack.acknowledges_all(len(segments))


@pytest.mark.asyncio
async def test_replayed_unsegmented_access_message_is_dispatched_once() -> None:
    received: list[AccessMessage] = []
    proxy = ProxyClient(
        NoopClient(),
        net_key=NET_KEY,
        app_key=APP_KEY,
        device_keys={},
        local_address=1,
        on_message=received.append,
    )
    seq = 10
    access_pdu = network.encode_opcode(0x26) + b"0123456789"
    upper = network.encrypt_access_payload(
        APP_KEY,
        device_key=False,
        iv_index=0,
        seq=seq,
        src=2,
        dst=1,
        access_pdu=access_pdu,
    )
    wire = network.encode_network_pdu(
        proxy._keys,
        iv_index=0,
        ctl=0,
        ttl=5,
        seq=seq,
        src=2,
        dst=1,
        transport_pdu=network.build_unsegmented_access(1, crypto.k4(APP_KEY), upper),
    )

    proxy._handle_network(wire)
    proxy._handle_network(wire)

    assert len(received) == 1
    assert proxy._replay_list == {2: seq}


@pytest.mark.asyncio
async def test_segment_retransmission_is_allowed_until_reassembly_completes() -> None:
    received: list[AccessMessage] = []
    proxy = ProxyClient(
        NoopClient(),
        net_key=NET_KEY,
        app_key=APP_KEY,
        device_keys={},
        local_address=1,
        sequence=100,
        on_message=received.append,
    )
    transport = RecordingTransport()
    proxy._transport = transport  # type: ignore[assignment]
    first_seq = 0x345
    access_pdu = network.encode_opcode(0x26) + bytes(range(25))
    upper = network.encrypt_access_payload(
        APP_KEY,
        device_key=False,
        iv_index=0,
        seq=first_seq,
        src=2,
        dst=1,
        access_pdu=access_pdu,
    )
    segments = network.build_access_segments(
        1, crypto.k4(APP_KEY), first_seq & 0x1FFF, upper
    )

    def deliver(index: int, seq: int) -> None:
        wire = network.encode_network_pdu(
            proxy._keys,
            iv_index=0,
            ctl=0,
            ttl=5,
            seq=seq,
            src=2,
            dst=1,
            transport_pdu=segments[index],
        )
        proxy._handle_network(wire)

    deliver(0, first_seq)
    deliver(0, first_seq)  # same Network PDU before the RPL can advance
    deliver(2, first_seq + 2)
    deliver(1, first_seq + 1)
    deliver(0, first_seq)  # completed transaction replay: ACK, no dispatch
    await asyncio.gather(*proxy._background_tasks)

    assert len(received) == 1
    assert proxy._replay_list[2] == first_seq + 2


@pytest.mark.asyncio
async def test_delayed_segmented_replay_is_acknowledged_but_not_dispatched() -> None:
    received: list[AccessMessage] = []
    proxy = ProxyClient(
        NoopClient(),
        net_key=NET_KEY,
        app_key=APP_KEY,
        device_keys={},
        local_address=1,
        sequence=100,
        on_message=received.append,
    )
    now = [0.0]
    proxy._reassembler = network.SegmentReassembler(
        timeout=10,
        clock=lambda: now[0],
    )
    transport = RecordingTransport()
    proxy._transport = transport  # type: ignore[assignment]

    first_seq = 0x345
    access_pdu = network.encode_opcode(0x26) + bytes(range(25))
    upper = network.encrypt_access_payload(
        APP_KEY,
        device_key=False,
        iv_index=0,
        seq=first_seq,
        src=2,
        dst=1,
        access_pdu=access_pdu,
    )
    segments = network.build_access_segments(
        1, crypto.k4(APP_KEY), first_seq & 0x1FFF, upper
    )

    def deliver(index: int, seq: int) -> None:
        wire = network.encode_network_pdu(
            proxy._keys,
            iv_index=0,
            ctl=0,
            ttl=5,
            seq=seq,
            src=2,
            dst=1,
            transport_pdu=segments[index],
        )
        proxy._handle_network(wire)

    for index in range(len(segments)):
        deliver(index, first_seq + index)
    await asyncio.gather(*proxy._background_tasks)
    assert len(received) == 1

    transport.sent.clear()
    now[0] = 11
    for index in range(len(segments)):
        deliver(index, first_seq + len(segments) + index)
    await asyncio.gather(*proxy._background_tasks)

    assert len(received) == 1
    assert len(transport.sent) == len(segments)
    for msg_type, wire in transport.sent:
        assert msg_type == TYPE_NETWORK
        ack_message = network.decode_network_pdu(proxy._keys, 0, wire)
        ack = network.parse_segment_ack(ack_message.transport_pdu[1:])
        assert ack.seq_zero == first_seq
        assert ack.acknowledges_all(len(segments))


@pytest.mark.asyncio
async def test_inbound_replay_state_survives_proxy_reconstruction() -> None:
    """A BLE reconnect must not reset replay state for the same mesh/IV Index."""
    received: list[AccessMessage] = []
    replay_list: dict[int, int] = {}
    reassembler = network.SegmentReassembler()

    def make_proxy(sequence: int) -> tuple[ProxyClient, RecordingTransport]:
        proxy = ProxyClient(
            NoopClient(),
            net_key=NET_KEY,
            app_key=APP_KEY,
            device_keys={},
            local_address=1,
            sequence=sequence,
            on_message=received.append,
            replay_list=replay_list,
            reassembler=reassembler,
        )
        transport = RecordingTransport()
        proxy._transport = transport  # type: ignore[assignment]
        return proxy, transport

    first_seq = 0x345
    access_pdu = network.encode_opcode(0x26) + bytes(range(25))
    upper = network.encrypt_access_payload(
        APP_KEY,
        device_key=False,
        iv_index=0,
        seq=first_seq,
        src=2,
        dst=1,
        access_pdu=access_pdu,
    )
    segments = network.build_access_segments(
        1, crypto.k4(APP_KEY), first_seq & 0x1FFF, upper
    )

    def deliver(proxy: ProxyClient, index: int, seq: int) -> None:
        wire = network.encode_network_pdu(
            proxy._keys,
            iv_index=0,
            ctl=0,
            ttl=5,
            seq=seq,
            src=2,
            dst=1,
            transport_pdu=segments[index],
        )
        proxy._handle_network(wire)

    first_proxy, _ = make_proxy(100)
    for index in range(len(segments)):
        deliver(first_proxy, index, first_seq + index)
    await asyncio.gather(*first_proxy._background_tasks)
    assert len(received) == 1

    second_proxy, second_transport = make_proxy(200)
    for index in range(len(segments)):
        deliver(second_proxy, index, first_seq + len(segments) + index)
    await asyncio.gather(*second_proxy._background_tasks)

    assert len(received) == 1
    assert second_proxy._replay_list is replay_list
    assert second_proxy._reassembler is reassembler
    assert len(second_transport.sent) == len(segments)


@pytest.mark.asyncio
async def test_segment_ack_requires_matching_route_and_new_valid_bits() -> None:
    proxy = ProxyClient(
        NoopClient(),
        net_key=NET_KEY,
        app_key=APP_KEY,
        device_keys={},
        local_address=1,
    )
    loop = asyncio.get_running_loop()
    pending = _PendingSegmentAck(loop.create_future(), dst=2, segment_count=3)
    proxy._pending_ack[5] = pending

    def deliver(
        *, src: int, dst: int, seq: int, bitmap: int, obo: bool = False
    ) -> None:
        field = (5 << 2) | (int(obo) << 15)
        params = field.to_bytes(2, "big") + bitmap.to_bytes(4, "big")
        wire = network.encode_network_pdu(
            proxy._keys,
            iv_index=0,
            ctl=1,
            ttl=0,
            seq=seq,
            src=src,
            dst=dst,
            transport_pdu=bytes([network.CONTROL_OPCODE_SEGMENT_ACK]) + params,
        )
        proxy._handle_network(wire)

    deliver(src=3, dst=1, seq=500, bitmap=0b001)
    deliver(src=3, dst=1, seq=500, bitmap=0b001, obo=True)
    deliver(src=2, dst=3, seq=501, bitmap=0b001)
    deliver(src=2, dst=1, seq=499, bitmap=0b1000)
    assert not pending.future.done()

    # Rejected ACKs do not poison the RPL; a lower-sequence valid one is accepted.
    deliver(src=2, dst=1, seq=498, bitmap=0b010)
    assert (await pending.future).block_ack == 0b010
    assert proxy._replay_list[2] == 498

    pending.future = loop.create_future()
    deliver(src=2, dst=1, seq=499, bitmap=0b010)  # no new progress
    assert not pending.future.done()
    deliver(src=2, dst=1, seq=500, bitmap=0b111)
    assert (await pending.future).block_ack == 0b111


@pytest.mark.asyncio
async def test_zero_segment_ack_cancels_outbound_transaction() -> None:
    proxy = ProxyClient(
        NoopClient(),
        net_key=NET_KEY,
        app_key=APP_KEY,
        device_keys={},
        local_address=1,
        sequence=200,
    )

    class CancelingTransport:
        async def send(self, msg_type: int, payload: bytes) -> None:
            assert msg_type == TYPE_NETWORK
            message = network.decode_network_pdu(proxy._keys, 0, payload)
            field = int.from_bytes(message.transport_pdu[1:4], "big")
            seq_zero = (field >> 10) & 0x1FFF
            seg_o = (field >> 5) & 0x1F
            seg_n = field & 0x1F
            if seg_o != seg_n:
                return
            params = (seq_zero << 2).to_bytes(2, "big") + bytes(4)
            ack_pdu = network.encode_network_pdu(
                proxy._keys,
                iv_index=0,
                ctl=1,
                ttl=0,
                seq=500,
                src=2,
                dst=1,
                transport_pdu=bytes([network.CONTROL_OPCODE_SEGMENT_ACK]) + params,
            )
            proxy._handle_network(ack_pdu)

    proxy._transport = CancelingTransport()  # type: ignore[assignment]
    with pytest.raises(ProxyError, match="canceled by receiver"):
        await proxy.send_access(2, 0x26, bytes(range(20)))


@pytest.mark.asyncio
async def test_request_uses_response_matcher() -> None:
    proxy = ProxyClient(
        NoopClient(),
        net_key=NET_KEY,
        app_key=APP_KEY,
        device_keys={},
    )

    async def send_access(*_args, **_kwargs) -> None:
        proxy._dispatch(AccessMessage(2, 1, 0x803E, b"wrong", False))
        await asyncio.sleep(0)
        proxy._dispatch(AccessMessage(2, 1, 0x803E, b"right", False))

    proxy.send_access = send_access  # type: ignore[method-assign]

    reply = await proxy.request(
        2,
        0x803D,
        b"request",
        expect_opcode=0x803E,
        response_matcher=lambda message: message.parameters == b"right",
    )

    assert reply.parameters == b"right"


@pytest.mark.asyncio
async def test_concurrent_requests_are_correlated_to_their_response() -> None:
    proxy = ProxyClient(
        NoopClient(),
        net_key=NET_KEY,
        app_key=APP_KEY,
        device_keys={},
    )
    both_sends_started = asyncio.Event()
    send_count = 0

    async def send_access(*_args, **_kwargs) -> None:
        nonlocal send_count
        send_count += 1
        if send_count == 2:
            both_sends_started.set()
        await both_sends_started.wait()

    proxy.send_access = send_access  # type: ignore[method-assign]

    alpha = asyncio.create_task(
        proxy.request(
            2,
            0x803D,
            b"alpha request",
            expect_opcode=0x803E,
            response_matcher=lambda message: message.parameters == b"alpha",
        )
    )
    beta = asyncio.create_task(
        proxy.request(
            2,
            0x803D,
            b"beta request",
            expect_opcode=0x803E,
            response_matcher=lambda message: message.parameters == b"beta",
        )
    )
    await asyncio.wait_for(both_sends_started.wait(), timeout=1)

    # A matching opcode and payload from another node must not satisfy either
    # waiter. Replies from the intended node can then arrive out of order.
    proxy._dispatch(AccessMessage(3, 1, 0x803E, b"alpha", True))
    proxy._dispatch(AccessMessage(2, 1, 0x803E, b"beta", True))
    proxy._dispatch(AccessMessage(2, 1, 0x803E, b"alpha", True))

    alpha_reply, beta_reply = await asyncio.gather(alpha, beta)
    assert alpha_reply.parameters == b"alpha"
    assert beta_reply.parameters == b"beta"
    assert proxy._responses == []


@pytest.mark.asyncio
async def test_failed_sequence_persistence_prevents_transmission_and_reuses_number() -> (
    None
):
    saves: list[dict[str, int]] = []

    async def verified_save(data: dict[str, int]) -> None:
        saves.append(data)
        if len(saves) == 1:
            raise OSError("sequence read-back did not match")

    reservation = SequenceReservation.create({}, verified_save, block_size=4)
    proxy = ProxyClient(
        NoopClient(),
        net_key=NET_KEY,
        app_key=APP_KEY,
        device_keys={},
        sequence=reservation.next_sequence,
        before_sequence=reservation.ensure_reserved,
        on_sequence=reservation.mark_next,
    )
    transport = RecordingTransport()
    proxy._transport = transport  # type: ignore[assignment]

    # Creating the reservation is lazy: no durable write happens until the
    # proxy is about to consume its first sequence number.
    assert saves == []
    with pytest.raises(OSError, match="read-back"):
        await proxy.send_access(2, 0x26, retries=1)

    assert transport.sent == []
    assert proxy.sequence == 4
    assert reservation.next_sequence == 4
    assert reservation.reserved_until == 4

    await proxy.send_access(2, 0x26, retries=1)

    assert saves == [
        {"reserved_until": 8, "sequence": 8},
        {"reserved_until": 8, "sequence": 8},
    ]
    assert reservation.reserved_until == 8
    assert reservation.next_sequence == 5
    assert proxy.sequence == 5
    sent_message = network.decode_network_pdu(proxy._keys, 0, transport.sent[0][1])
    assert sent_message.seq == 4


@pytest.mark.asyncio
async def test_segmented_send_retries_only_segments_missing_from_ack() -> None:
    proxy = ProxyClient(
        NoopClient(),
        net_key=NET_KEY,
        app_key=APP_KEY,
        device_keys={},
        local_address=1,
        sequence=200,
    )
    sent: list[tuple[int, int, int, int]] = []

    class PartialAckTransport:
        async def send(self, msg_type: int, payload: bytes) -> None:
            assert msg_type == TYPE_NETWORK
            message = network.decode_network_pdu(proxy._keys, 0, payload)
            field = int.from_bytes(message.transport_pdu[1:4], "big")
            seq_zero = (field >> 10) & 0x1FFF
            seg_o = (field >> 5) & 0x1F
            seg_n = field & 0x1F
            sent.append((message.seq, seq_zero, seg_o, seg_n))

            full_ack = (1 << (seg_n + 1)) - 1
            if len(sent) == seg_n + 1:
                self._deliver_ack(seq_zero, full_ack & ~(1 << 1))
            elif len(sent) == seg_n + 2:
                self._deliver_ack(seq_zero, full_ack)

        def _deliver_ack(self, seq_zero: int, block_ack: int) -> None:
            params = (seq_zero << 2).to_bytes(2, "big") + block_ack.to_bytes(4, "big")
            ack_pdu = network.encode_network_pdu(
                proxy._keys,
                iv_index=0,
                ctl=1,
                ttl=0,
                seq=500 + len(sent),
                src=2,
                dst=1,
                transport_pdu=bytes([network.CONTROL_OPCODE_SEGMENT_ACK]) + params,
            )
            proxy._handle_network(ack_pdu)

    proxy._transport = PartialAckTransport()  # type: ignore[assignment]

    await proxy.send_access(2, 0x26, bytes(range(20)))

    assert [seg_o for _seq, _seq_zero, seg_o, _seg_n in sent] == [0, 1, 2, 1]
    assert [seq for seq, _seq_zero, _seg_o, _seg_n in sent] == [200, 201, 202, 203]
    assert {seq_zero for _seq, seq_zero, _seg_o, _seg_n in sent} == {200}
    assert proxy.sequence == 204
    assert proxy._pending_ack == {}
