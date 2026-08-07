"""Proxy PDU framing over GATT.

Both the Mesh Provisioning Service (0x1827) and the Mesh Proxy Service (0x1828)
carry the same one-byte-header "Proxy PDU" framing described in Mesh Profile
1.0.1 section 6.3, so a single transport serves both: only the characteristic
UUIDs differ.
"""

from __future__ import annotations

import asyncio
import contextlib
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
        self._write_lock = asyncio.Lock()

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
        await self._client.start_notify(self._char_out, self._notification_handler)

    async def stop(self) -> None:
        # Teardown must not mask whatever error prompted it.
        with contextlib.suppress(Exception):
            await self._client.stop_notify(self._char_out)

    async def send(self, msg_type: int, payload: bytes) -> None:
        """Write one Proxy PDU, segmenting it if it exceeds one write."""
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
        await self._client.write_gatt_char(self._char_in, data, response=False)

    def _notification_handler(self, _sender, data: bytearray) -> None:
        _LOGGER.debug("RX %s", bytes(data).hex())
        if not data:
            return
        sar = (data[0] >> 6) & 0x03
        msg_type = data[0] & 0x3F
        body = bytes(data[1:])

        if sar == SAR_COMPLETE:
            self._dispatch(msg_type, body)
            return
        if sar == SAR_FIRST:
            self._rx_type = msg_type
            self._rx_buf = bytearray(body)
            return
        if self._rx_type != msg_type:
            # A continuation with no matching first segment: drop the fragment
            # rather than splicing it onto an unrelated message.
            _LOGGER.debug("discarding orphan SAR fragment for type %#x", msg_type)
            return
        self._rx_buf += body
        if sar == SAR_LAST:
            self._dispatch(msg_type, bytes(self._rx_buf))
            self._rx_type = None
            self._rx_buf = bytearray()

    def _dispatch(self, msg_type: int, payload: bytes) -> None:
        result = self._on_message(msg_type, payload)
        if asyncio.iscoroutine(result):
            # Notification callbacks are sync; hand async handlers to the loop.
            asyncio.get_running_loop().create_task(result)
