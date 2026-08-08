"""Proxy PDU framing over GATT.

Both the Mesh Provisioning Service (0x1827) and the Mesh Proxy Service (0x1828)
carry the same one-byte-header "Proxy PDU" framing described in Mesh Profile
1.0.1 section 6.3, so a single transport serves both: only the characteristic
UUIDs differ.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
from collections.abc import Awaitable, Callable

_LOGGER = logging.getLogger(__name__)

MESH_PROVISIONING_SERVICE = "00001827-0000-1000-8000-00805f9b34fb"
MESH_PROXY_SERVICE = "00001828-0000-1000-8000-00805f9b34fb"

PROVISIONING_DATA_IN = "00002adb-0000-1000-8000-00805f9b34fb"
PROVISIONING_DATA_OUT = "00002adc-0000-1000-8000-00805f9b34fb"
PROXY_DATA_IN = "00002add-0000-1000-8000-00805f9b34fb"
PROXY_DATA_OUT = "00002ade-0000-1000-8000-00805f9b34fb"

# Proxy PDU message types (low 6 bits of the header byte).
TYPE_NETWORK = 0x00
TYPE_MESH_BEACON = 0x01
TYPE_PROXY_CONFIGURATION = 0x02
TYPE_PROVISIONING = 0x03

# SAR field (high 2 bits of the header byte).
SAR_COMPLETE = 0x00
SAR_FIRST = 0x01
SAR_CONTINUATION = 0x02
SAR_LAST = 0x03

# BlueZ normally bounds D-Bus operations, but broken fixture proxy firmware can
# leave notification or write calls pending forever. Never let one GATT call
# stall config-entry setup or unload indefinitely.
GATT_OPERATION_TIMEOUT = 10.0
PROXY_SAR_TIMEOUT = 20.0

# Maximum Data-field lengths from Mesh Protocol 1.1. Keeping these bounds at
# the bearer prevents a broken or hostile proxy from growing a SAR buffer
# without limit before the authenticated protocol layer can inspect it. Mesh
# Private beacons increased the beacon maximum from 23 to 27 octets in 1.1.
MAX_PROXY_MESSAGE_LENGTH = {
    TYPE_NETWORK: 29,
    TYPE_MESH_BEACON: 27,
    TYPE_PROXY_CONFIGURATION: 29,
    TYPE_PROVISIONING: 65,
}


class MeshGattTransport:
    """Segments outgoing / reassembles incoming Proxy PDUs on a bleak client."""

    def __init__(
        self,
        client,
        char_in: str,
        char_out: str,
        on_message: Callable[[int, bytes], Awaitable[None] | None],
    ) -> None:
        self._client = client
        self._char_in = char_in
        self._char_out = char_out
        self._on_message = on_message
        self._rx_type: int | None = None
        self._rx_buf = bytearray()
        self._rx_timeout: asyncio.TimerHandle | None = None
        self._protocol_failed = False
        self._write_lock = asyncio.Lock()
        self._background_tasks: set[asyncio.Task] = set()

    # Bytes of PDU payload per write. Every LE link guarantees a 23-byte ATT
    # MTU, leaving 20 bytes for a write-without-response and 19 after our own
    # header byte.
    #
    # Deliberately fixed rather than negotiated: reading a client's MTU makes
    # bleak's BlueZ backend warn whenever the value is not yet known, and Home
    # Assistant's client wrapper routes even the private attribute to that same
    # property. Mesh PDUs are tens of bytes, so the most a larger MTU could
    # save is one or two writes per message.
    SEGMENT_SIZE = 19

    async def start(self) -> None:
        self._reset_rx()
        self._protocol_failed = False
        async with asyncio.timeout(GATT_OPERATION_TIMEOUT):
            await self._client.start_notify(self._char_out, self._notification_handler)

    async def stop(self) -> None:
        self._reset_rx()
        for task in self._background_tasks:
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()
        # Teardown must not mask whatever error prompted it.
        with contextlib.suppress(Exception):
            async with asyncio.timeout(GATT_OPERATION_TIMEOUT):
                await self._client.stop_notify(self._char_out)

    async def send(self, msg_type: int, payload: bytes) -> None:
        """Write one Proxy PDU, segmenting it if it exceeds one write."""
        limit = MAX_PROXY_MESSAGE_LENGTH.get(msg_type)
        if limit is None:
            raise ValueError(f"unsupported Proxy PDU message type {msg_type:#04x}")
        if len(payload) > limit:
            raise ValueError(
                f"Proxy PDU type {msg_type:#04x} exceeds {limit}-byte limit"
            )
        size = self.SEGMENT_SIZE
        async with self._write_lock:
            if len(payload) <= size:
                await self._write(bytes([(SAR_COMPLETE << 6) | msg_type]) + payload)
                return
            chunks = [payload[i : i + size] for i in range(0, len(payload), size)]
            for index, chunk in enumerate(chunks):
                if index == 0:
                    sar = SAR_FIRST
                elif index == len(chunks) - 1:
                    sar = SAR_LAST
                else:
                    sar = SAR_CONTINUATION
                await self._write(bytes([(sar << 6) | msg_type]) + chunk)

    async def _write(self, data: bytes) -> None:
        _LOGGER.debug("TX %s", data.hex())
        async with asyncio.timeout(GATT_OPERATION_TIMEOUT):
            await self._client.write_gatt_char(self._char_in, data, response=False)

    def _notification_handler(self, _sender, data: bytearray) -> None:
        _LOGGER.debug("RX %s", bytes(data).hex())
        if not data:
            return
        if self._protocol_failed:
            return
        sar = (data[0] >> 6) & 0x03
        msg_type = data[0] & 0x3F
        body = bytes(data[1:])
        limit = MAX_PROXY_MESSAGE_LENGTH.get(msg_type)
        if limit is None:
            # RFU and unsupported message types are ignored by specification.
            return

        if sar == SAR_COMPLETE:
            if self._rx_type is not None:
                self._fail_protocol("complete Proxy PDU interrupted a SAR transfer")
                return
            if len(body) > limit:
                self._fail_protocol("complete Proxy PDU exceeds its maximum length")
                return
            self._dispatch(msg_type, body)
            return
        if sar == SAR_FIRST:
            if self._rx_type is not None:
                self._fail_protocol("nested first Proxy SAR segment")
                return
            if len(body) > limit:
                self._fail_protocol("first Proxy SAR segment exceeds message limit")
                return
            self._rx_type = msg_type
            self._rx_buf = bytearray(body)
            self._rx_timeout = asyncio.get_running_loop().call_later(
                PROXY_SAR_TIMEOUT, self._sar_timed_out
            )
            return
        if self._rx_type != msg_type:
            self._fail_protocol(
                f"orphan or mismatched Proxy SAR fragment for type {msg_type:#x}"
            )
            return
        if len(self._rx_buf) + len(body) > limit:
            self._fail_protocol("reassembled Proxy PDU exceeds its maximum length")
            return
        self._rx_buf += body
        if sar == SAR_LAST:
            payload = bytes(self._rx_buf)
            self._reset_rx()
            self._dispatch(msg_type, payload)

    def _reset_rx(self) -> None:
        if self._rx_timeout is not None:
            self._rx_timeout.cancel()
            self._rx_timeout = None
        self._rx_type = None
        self._rx_buf = bytearray()

    def _sar_timed_out(self) -> None:
        self._rx_timeout = None
        self._fail_protocol("Proxy SAR transfer timed out")

    def _fail_protocol(self, reason: str) -> None:
        _LOGGER.debug("disconnecting after invalid Proxy SAR: %s", reason)
        self._reset_rx()
        self._protocol_failed = True

        async def disconnect_best_effort() -> None:
            disconnect = getattr(self._client, "disconnect", None)
            if disconnect is None:
                return
            try:
                async with asyncio.timeout(GATT_OPERATION_TIMEOUT):
                    result = disconnect()
                    if inspect.isawaitable(result):
                        await result
            except Exception as err:
                _LOGGER.debug("could not disconnect invalid Proxy bearer: %s", err)

        # A buggy backend must not keep an orphan disconnect task alive forever.
        self._schedule(disconnect_best_effort())

    def _schedule(self, awaitable: Awaitable) -> None:
        task = asyncio.get_running_loop().create_task(awaitable)
        self._background_tasks.add(task)

        def done(completed: asyncio.Task) -> None:
            self._background_tasks.discard(completed)
            if completed.cancelled():
                return
            with contextlib.suppress(Exception):
                if err := completed.exception():
                    _LOGGER.debug("background GATT callback failed: %s", err)

        task.add_done_callback(done)

    def _dispatch(self, msg_type: int, payload: bytes) -> None:
        result = self._on_message(msg_type, payload)
        if inspect.isawaitable(result):
            # Notification callbacks are sync; hand async handlers to the loop.
            self._schedule(result)
