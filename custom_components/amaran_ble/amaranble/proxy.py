"""A GATT Proxy Client: sends and receives mesh access messages over a connection.

Owns the sequence number, the proxy filter handshake, lower-transport
segmentation with retransmission, and reassembly of inbound messages.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from . import crypto, network
from .gatt import (
    PROXY_DATA_IN,
    PROXY_DATA_OUT,
    TYPE_NETWORK,
    TYPE_PROXY_CONFIGURATION,
    MeshGattTransport,
)
from .network import NetworkDecodeError, NetworkKeys
from .sequence import MAX_SEQUENCE

_LOGGER = logging.getLogger(__name__)

PROXY_CONFIG_SET_FILTER_TYPE = 0x00
PROXY_CONFIG_ADD_ADDRESSES = 0x01
PROXY_CONFIG_FILTER_STATUS = 0x03
PROXY_FILTER_ACCEPT_LIST = 0x00

DEFAULT_TTL = 5
PROXY_START_TIMEOUT = 30.0


class ProxyError(Exception):
    """A mesh message could not be delivered."""


@dataclass(frozen=True)
class AccessMessage:
    """An inbound access message, after all layers have been peeled off."""

    src: int
    dst: int
    opcode: int
    parameters: bytes
    device_key: bool


class ProxyClient:
    """Speaks mesh over one connected fixture acting as a GATT Proxy Server."""

    def __init__(
        self,
        client,
        *,
        net_key: bytes,
        app_key: bytes,
        device_keys: dict[int, bytes],
        local_address: int = 0x0001,
        iv_index: int = 0,
        sequence: int = 0,
        on_message: Callable[[AccessMessage], None] | None = None,
        on_sequence: Callable[[int], None] | None = None,
        before_sequence: Callable[[int], Awaitable[None]] | None = None,
    ) -> None:
        self._keys = NetworkKeys.derive(net_key)
        self._app_key = app_key
        self._aid = crypto.k4(app_key)
        self._device_keys = device_keys
        self._local_address = local_address
        self._iv_index = iv_index
        self._seq = sequence
        self._on_message = on_message
        self._on_sequence = on_sequence
        self._before_sequence = before_sequence

        self._transport = MeshGattTransport(
            client, PROXY_DATA_IN, PROXY_DATA_OUT, self._on_proxy_pdu
        )
        self._reassembler = network.SegmentReassembler()
        self._send_lock = asyncio.Lock()
        self._sequence_lock = asyncio.Lock()
        self._filter_status: asyncio.Future[None] | None = None
        self._pending_ack: dict[int, asyncio.Future[network.SegmentAck]] = {}
        self._responses: list[
            tuple[Callable[[AccessMessage], bool], asyncio.Future]
        ] = []
        self._background_tasks: set[asyncio.Task[None]] = set()

    @property
    def sequence(self) -> int:
        return self._seq

    async def _next_seq(self) -> int:
        async with self._sequence_lock:
            if self._seq > MAX_SEQUENCE:
                raise ProxyError(
                    "Bluetooth Mesh sequence numbers are exhausted for this IV Index"
                )
            if self._before_sequence:
                await self._before_sequence(self._seq)
            seq = self._seq
            self._seq += 1
            if self._on_sequence:
                self._on_sequence(self._seq)
            return seq

    async def start(self, subscribe_addresses: list[int] | None = None) -> None:
        """Subscribe to notifications and install the proxy filter.

        The filter starts empty, so without this the proxy server forwards
        nothing back to us and every status message would be lost.
        """
        async with asyncio.timeout(PROXY_START_TIMEOUT):
            await self._transport.start()
            # Telink firmware needs a short pause after enabling notifications.
            await asyncio.sleep(0.5)
            await self._setup_filter(subscribe_addresses or [])

    async def stop(self) -> None:
        for task in self._background_tasks:
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()
        await self._transport.stop()

    # ─── Outbound ────────────────────────────────────────────────────────────

    async def send_access(
        self,
        dst: int,
        opcode: int,
        parameters: bytes = b"",
        *,
        device_key_for: int | None = None,
        ttl: int = DEFAULT_TTL,
        retries: int = 4,
    ) -> None:
        """Send one access message, segmenting and retrying as needed."""
        access_pdu = network.encode_opcode(opcode) + parameters

        if device_key_for is not None:
            key = self._device_keys.get(device_key_for)
            if key is None:
                raise ProxyError(f"no device key known for {device_key_for:#06x}")
            akf, aid, use_device_key = 0, 0, True
        else:
            key, akf, aid, use_device_key = self._app_key, 1, self._aid, False

        async with self._send_lock:
            if len(access_pdu) <= network.UNSEGMENTED_ACCESS_MAX:
                await self._send_unsegmented(
                    dst, access_pdu, key, akf, aid, use_device_key, ttl, retries
                )
            else:
                await self._send_segmented(
                    dst, access_pdu, key, akf, aid, use_device_key, ttl
                )

    async def _send_unsegmented(
        self,
        dst: int,
        access_pdu: bytes,
        key: bytes,
        akf: int,
        aid: int,
        use_device_key: bool,
        ttl: int,
        retries: int,
    ) -> None:
        # Unsegmented messages are unacknowledged at the transport layer, so a
        # few repeats are the only defence against a dropped advertising slot.
        # Each repeat needs a fresh sequence number or the node replays-filters it.
        for attempt in range(max(1, retries)):
            seq = await self._next_seq()
            upper = network.encrypt_access_payload(
                key,
                device_key=use_device_key,
                iv_index=self._iv_index,
                seq=seq,
                src=self._local_address,
                dst=dst,
                access_pdu=access_pdu,
            )
            pdu = network.encode_network_pdu(
                self._keys,
                iv_index=self._iv_index,
                ctl=0,
                ttl=ttl,
                seq=seq,
                src=self._local_address,
                dst=dst,
                transport_pdu=network.build_unsegmented_access(akf, aid, upper),
            )
            await self._transport.send(TYPE_NETWORK, pdu)
            if attempt + 1 < max(1, retries):
                await asyncio.sleep(0.06)

    async def _send_segmented(
        self,
        dst: int,
        access_pdu: bytes,
        key: bytes,
        akf: int,
        aid: int,
        use_device_key: bool,
        ttl: int,
    ) -> None:
        seq_zero_seq = await self._next_seq()
        upper = network.encrypt_access_payload(
            key,
            device_key=use_device_key,
            iv_index=self._iv_index,
            seq=seq_zero_seq,
            src=self._local_address,
            dst=dst,
            access_pdu=access_pdu,
        )
        seq_zero = seq_zero_seq & 0x1FFF
        segments = network.build_access_segments(akf, aid, seq_zero, upper)

        loop = asyncio.get_running_loop()
        ack_future: asyncio.Future[network.SegmentAck] = loop.create_future()
        self._pending_ack[seq_zero] = ack_future
        try:
            outstanding = set(range(len(segments)))
            for attempt in range(4):
                for index in sorted(outstanding):
                    seq = (
                        seq_zero_seq
                        if (attempt == 0 and index == 0)
                        else await self._next_seq()
                    )
                    pdu = network.encode_network_pdu(
                        self._keys,
                        iv_index=self._iv_index,
                        ctl=0,
                        ttl=ttl,
                        seq=seq,
                        src=self._local_address,
                        dst=dst,
                        transport_pdu=segments[index],
                    )
                    await self._transport.send(TYPE_NETWORK, pdu)
                    await asyncio.sleep(0.03)

                # Group and broadcast destinations are never acknowledged.
                if dst >= 0x8000:
                    return
                try:
                    ack = await asyncio.wait_for(
                        asyncio.shield(ack_future), timeout=1.5
                    )
                except TimeoutError:
                    continue
                outstanding = {
                    i for i in range(len(segments)) if not ack.block_ack & (1 << i)
                }
                if not outstanding:
                    return
                ack_future = loop.create_future()
                self._pending_ack[seq_zero] = ack_future
            raise ProxyError(f"segmented message to {dst:#06x} was not acknowledged")
        finally:
            self._pending_ack.pop(seq_zero, None)

    async def request(
        self,
        dst: int,
        opcode: int,
        parameters: bytes,
        *,
        expect_opcode: int,
        device_key_for: int | None = None,
        response_matcher: Callable[[AccessMessage], bool] | None = None,
        retries: int = 4,
        timeout: float = 6.0,
    ) -> AccessMessage:
        """Send a message and wait for a matching reply."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[AccessMessage] = loop.create_future()

        def matches(message: AccessMessage) -> bool:
            return (
                message.src == dst
                and message.opcode == expect_opcode
                and (response_matcher is None or response_matcher(message))
            )

        matcher = (matches, future)
        self._responses.append(matcher)
        try:
            await self.send_access(
                dst,
                opcode,
                parameters,
                device_key_for=device_key_for,
                retries=retries,
            )
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            raise ProxyError(f"no {expect_opcode:#06x} reply from {dst:#06x}") from None
        finally:
            if matcher in self._responses:
                self._responses.remove(matcher)

    async def _setup_filter(self, addresses: list[int]) -> None:
        loop = asyncio.get_running_loop()
        self._filter_status = loop.create_future()
        await self._send_proxy_config(
            PROXY_CONFIG_SET_FILTER_TYPE, bytes([PROXY_FILTER_ACCEPT_LIST])
        )
        try:
            await asyncio.wait_for(asyncio.shield(self._filter_status), timeout=2.0)
        except TimeoutError:
            _LOGGER.debug("no filter status after Set Filter Type; continuing")

        wanted = [self._local_address, network.ALL_NODES_ADDRESS, *addresses]
        payload = b"".join(a.to_bytes(2, "big") for a in dict.fromkeys(wanted))
        self._filter_status = loop.create_future()
        await self._send_proxy_config(PROXY_CONFIG_ADD_ADDRESSES, payload)
        try:
            await asyncio.wait_for(asyncio.shield(self._filter_status), timeout=2.0)
        except TimeoutError:
            _LOGGER.debug("no filter status after Add Addresses; continuing")
        self._filter_status = None
        # Match the fixture's own control stack: it allows the updated filter
        # to settle before sending the first network message.
        await asyncio.sleep(0.3)

    async def _send_proxy_config(self, opcode: int, params: bytes) -> None:
        seq = await self._next_seq()
        pdu = network.encode_network_pdu(
            self._keys,
            iv_index=self._iv_index,
            ctl=1,
            ttl=0,
            seq=seq,
            src=self._local_address,
            dst=network.UNASSIGNED_ADDRESS,
            transport_pdu=bytes([opcode & 0x7F]) + params,
            proxy_config=True,
        )
        await self._transport.send(TYPE_PROXY_CONFIGURATION, pdu)

    # ─── Inbound ─────────────────────────────────────────────────────────────

    def _on_proxy_pdu(self, msg_type: int, payload: bytes) -> None:
        try:
            if msg_type == TYPE_PROXY_CONFIGURATION:
                self._handle_proxy_config(payload)
            elif msg_type == TYPE_NETWORK:
                self._handle_network(payload)
        except NetworkDecodeError as err:
            _LOGGER.debug("dropping PDU: %s", err)
        except Exception:
            _LOGGER.exception("error handling inbound PDU")

    def _handle_proxy_config(self, payload: bytes) -> None:
        message = network.decode_network_pdu(
            self._keys, self._iv_index, payload, proxy_config=True
        )
        opcode = message.transport_pdu[0] & 0x7F
        if opcode == PROXY_CONFIG_FILTER_STATUS:
            _LOGGER.debug("proxy filter status: %s", message.transport_pdu[1:].hex())
            if self._filter_status and not self._filter_status.done():
                self._filter_status.set_result(None)

    def _handle_network(self, payload: bytes) -> None:
        message = network.decode_network_pdu(self._keys, self._iv_index, payload)
        if message.src == self._local_address:
            return  # our own message relayed back

        lower = message.transport_pdu
        if not lower:
            return

        if message.ctl:
            opcode = lower[0] & 0x7F
            if opcode == network.CONTROL_OPCODE_SEGMENT_ACK:
                ack = network.parse_segment_ack(lower[1:])
                future = self._pending_ack.get(ack.seq_zero)
                if future and not future.done():
                    future.set_result(ack)
            return

        segmented = bool(lower[0] & 0x80)
        if segmented:
            field = int.from_bytes(lower[1:4], "big")
            seq_zero = (field >> 10) & 0x1FFF
            seg_o = (field >> 5) & 0x1F
            seg_n = field & 0x1F
            result = self._reassembler.add(message.src, message.seq, lower)
            if result is None:
                # The sender waits for an acknowledgment after its final
                # segment. A partial bitmap tells it exactly what to retry.
                if seg_o == seg_n and message.dst == self._local_address:
                    self._schedule_segment_ack(
                        message.src,
                        seq_zero,
                        self._reassembler.block_ack(message.src, seq_zero),
                    )
                return
            upper, akf, aid, szmic, seq_auth = result
            if message.dst == self._local_address:
                self._schedule_segment_ack(
                    message.src,
                    seq_zero,
                    (1 << (seg_n + 1)) - 1,
                )
        else:
            akf = (lower[0] >> 6) & 1
            aid = lower[0] & 0x3F
            upper, szmic, seq_auth = lower[1:], 0, message.seq

        self._decrypt_and_dispatch(message, upper, akf, aid, szmic, seq_auth)

    def _schedule_segment_ack(self, dst: int, seq_zero: int, block_ack: int) -> None:
        task = asyncio.get_running_loop().create_task(
            self._send_segment_ack(dst, seq_zero, block_ack)
        )
        self._background_tasks.add(task)

        def done(completed: asyncio.Task[None]) -> None:
            self._background_tasks.discard(completed)
            if completed.cancelled():
                return
            with contextlib.suppress(Exception):
                if err := completed.exception():
                    _LOGGER.debug("could not send segment acknowledgment: %s", err)

        task.add_done_callback(done)

    async def _send_segment_ack(self, dst: int, seq_zero: int, block_ack: int) -> None:
        seq = await self._next_seq()
        params = ((seq_zero & 0x1FFF) << 2).to_bytes(2, "big") + block_ack.to_bytes(
            4, "big"
        )
        pdu = network.encode_network_pdu(
            self._keys,
            iv_index=self._iv_index,
            ctl=1,
            ttl=0,
            seq=seq,
            src=self._local_address,
            dst=dst,
            transport_pdu=bytes([network.CONTROL_OPCODE_SEGMENT_ACK]) + params,
        )
        await self._transport.send(TYPE_NETWORK, pdu)

    def _decrypt_and_dispatch(
        self,
        message: network.NetworkMessage,
        upper: bytes,
        akf: int,
        aid: int,
        szmic: int,
        seq_auth: int,
    ) -> None:
        candidates: list[tuple[bytes, bool]] = []
        if akf == 0:
            device_key = self._device_keys.get(message.src)
            if device_key:
                candidates.append((device_key, True))
        elif aid == self._aid:
            candidates.append((self._app_key, False))

        for key, is_device_key in candidates:
            try:
                access_pdu = network.decrypt_access_payload(
                    key,
                    device_key=is_device_key,
                    iv_index=self._iv_index,
                    seq=seq_auth,
                    src=message.src,
                    dst=message.dst,
                    upper_pdu=upper,
                    szmic=szmic,
                )
            except NetworkDecodeError:
                continue
            opcode, parameters = network.decode_opcode(access_pdu)
            self._dispatch(
                AccessMessage(
                    src=message.src,
                    dst=message.dst,
                    opcode=opcode,
                    parameters=parameters,
                    device_key=is_device_key,
                )
            )
            return
        _LOGGER.debug(
            "no key decrypted message from %#06x (akf=%d aid=%#04x)",
            message.src,
            akf,
            aid,
        )

    def _dispatch(self, message: AccessMessage) -> None:
        _LOGGER.debug(
            "RX access src=%#06x opcode=%#06x params=%s",
            message.src,
            message.opcode,
            message.parameters.hex(),
        )
        for matcher, future in list(self._responses):
            if not future.done() and matcher(message):
                future.set_result(message)
        if self._on_message:
            try:
                self._on_message(message)
            except Exception:
                _LOGGER.exception("access message listener failed")
