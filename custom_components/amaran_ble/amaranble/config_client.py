"""Configuration Client: the foundation models needed to make a new node usable.

After provisioning, a node holds a NetKey but no application keys and no model
bindings, so it will ignore every light command until we add an AppKey and bind
it. That is all this module does.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .network import NetworkDecodeError
from .proxy import ProxyClient, ProxyError

_LOGGER = logging.getLogger(__name__)

COMPOSITION_DATA_GET = 0x8008
COMPOSITION_DATA_STATUS = 0x02
APPKEY_ADD = 0x00
APPKEY_STATUS = 0x8003
MODEL_APP_BIND = 0x803D
MODEL_APP_STATUS = 0x803E
DEFAULT_TTL_SET = 0x800E
DEFAULT_TTL_STATUS = 0x800D
NODE_RESET = 0x8049
NODE_RESET_STATUS = 0x804A

STATUS_SUCCESS = 0x00
STATUS_CODES = {
    0x01: "invalid address",
    0x02: "invalid model",
    0x03: "invalid app key index",
    0x04: "invalid net key index",
    0x05: "insufficient resources",
    0x06: "key index already stored",
    0x07: "invalid publish parameters",
    0x08: "not a subscribe model",
    0x09: "storage failure",
    0x0A: "feature not supported",
    0x0B: "cannot update",
    0x0C: "cannot remove",
    0x0D: "cannot bind",
    0x0E: "temporarily unable to change state",
    0x0F: "cannot set",
    0x10: "unspecified error",
    0x11: "invalid binding",
}


class ConfigError(Exception):
    """A configuration message was rejected by the node."""


@dataclass
class Element:
    address: int
    location: int
    sig_models: list[int] = field(default_factory=list)
    vendor_models: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class CompositionData:
    company_id: int
    product_id: int
    version_id: int
    crpl: int
    features: int
    elements: list[Element]

    @classmethod
    def parse(cls, data: bytes, base_address: int) -> CompositionData:
        if len(data) < 11 or data[0] != 0x00:
            raise ConfigError(f"unsupported composition data page: {data.hex()}")
        company_id = int.from_bytes(data[1:3], "little")
        product_id = int.from_bytes(data[3:5], "little")
        version_id = int.from_bytes(data[5:7], "little")
        crpl = int.from_bytes(data[7:9], "little")
        features = int.from_bytes(data[9:11], "little")

        elements: list[Element] = []
        offset = 11
        index = 0
        while offset + 4 <= len(data):
            location = int.from_bytes(data[offset : offset + 2], "little")
            num_sig = data[offset + 2]
            num_vendor = data[offset + 3]
            offset += 4
            element = Element(address=base_address + index, location=location)
            for _ in range(num_sig):
                if offset + 2 > len(data):
                    raise ConfigError("truncated composition data (SIG models)")
                element.sig_models.append(
                    int.from_bytes(data[offset : offset + 2], "little")
                )
                offset += 2
            for _ in range(num_vendor):
                if offset + 4 > len(data):
                    raise ConfigError("truncated composition data (vendor models)")
                element.vendor_models.append(
                    (
                        int.from_bytes(data[offset : offset + 2], "little"),
                        int.from_bytes(data[offset + 2 : offset + 4], "little"),
                    )
                )
                offset += 4
            elements.append(element)
            index += 1
        return cls(company_id, product_id, version_id, crpl, features, elements)


def _pack_key_indexes(net_key_index: int, app_key_index: int) -> bytes:
    """Two 12-bit key indexes packed into three octets (section 4.3.1.1)."""
    return ((net_key_index & 0xFFF) | ((app_key_index & 0xFFF) << 12)).to_bytes(
        3, "little"
    )


def _check_status(status: int, what: str) -> None:
    if status != STATUS_SUCCESS:
        raise ConfigError(
            f"{what} failed: {STATUS_CODES.get(status, f'status {status:#04x}')}"
        )


class ConfigClient:
    """Issues Configuration Server messages to one node, keyed by its DevKey."""

    def __init__(self, proxy: ProxyClient, address: int) -> None:
        self._proxy = proxy
        self._address = address

    async def get_composition_data(self, page: int = 0) -> CompositionData:
        reply = await self._proxy.request(
            self._address,
            COMPOSITION_DATA_GET,
            bytes([page]),
            expect_opcode=COMPOSITION_DATA_STATUS,
            device_key_for=self._address,
            response_matcher=lambda message: (
                bool(message.parameters) and message.parameters[0] == page
            ),
            timeout=10.0,
        )
        return CompositionData.parse(reply.parameters, self._address)

    async def add_app_key(
        self, app_key: bytes, *, net_key_index: int = 0, app_key_index: int = 0
    ) -> None:
        key_indexes = _pack_key_indexes(net_key_index, app_key_index)
        reply = await self._proxy.request(
            self._address,
            APPKEY_ADD,
            key_indexes + app_key,
            expect_opcode=APPKEY_STATUS,
            device_key_for=self._address,
            response_matcher=lambda message: message.parameters[1:4] == key_indexes,
        )
        # A key that is already stored is not an error for our purposes -- it
        # means a previous run got this far.
        if reply.parameters and reply.parameters[0] == 0x06:
            _LOGGER.debug("app key already present on %#06x", self._address)
            return
        _check_status(reply.parameters[0] if reply.parameters else 0xFF, "AppKey Add")

    async def bind_model(
        self,
        element_address: int,
        model: int | tuple[int, int],
        *,
        app_key_index: int = 0,
    ) -> None:
        if isinstance(model, tuple):
            company_id, model_id = model
            model_bytes = company_id.to_bytes(2, "little") + model_id.to_bytes(
                2, "little"
            )
        else:
            model_bytes = model.to_bytes(2, "little")
        params = (
            element_address.to_bytes(2, "little")
            + app_key_index.to_bytes(2, "little")
            + model_bytes
        )
        reply = await self._proxy.request(
            self._address,
            MODEL_APP_BIND,
            params,
            expect_opcode=MODEL_APP_STATUS,
            device_key_for=self._address,
            response_matcher=lambda message: message.parameters[1:] == params,
        )
        _check_status(
            reply.parameters[0] if reply.parameters else 0xFF,
            f"Model App Bind ({model})",
        )

    async def set_default_ttl(self, ttl: int) -> None:
        await self._proxy.request(
            self._address,
            DEFAULT_TTL_SET,
            bytes([ttl & 0x7F]),
            expect_opcode=DEFAULT_TTL_STATUS,
            device_key_for=self._address,
            response_matcher=lambda message: message.parameters == bytes([ttl & 0x7F]),
        )

    async def node_reset(self) -> bool:
        """Factory-reset the node so it can be re-provisioned (by us or the app)."""
        try:
            await self._proxy.request(
                self._address,
                NODE_RESET,
                b"",
                expect_opcode=NODE_RESET_STATUS,
                device_key_for=self._address,
                retries=1,
                timeout=4.0,
            )
        except (ProxyError, NetworkDecodeError):
            # Nodes commonly reset before the status reaches us. The caller
            # separately checks whether the BLE link dropped before deciding
            # that the reset succeeded.
            _LOGGER.debug("no node reset status from %#06x", self._address)
            return False
        return True

    async def bind_all_models(
        self, composition: CompositionData, *, app_key_index: int = 0
    ) -> None:
        """Bind our AppKey to every model that can accept one.

        The Telink firmware routes amaran's proprietary light opcode through a
        model we cannot identify from the composition data alone, so rather than
        guess we bind everything. Configuration models (0x0000-0x0001) reject
        bindings by design and are skipped.
        """
        for element in composition.elements:
            for model_id in element.sig_models:
                if model_id in (0x0000, 0x0001):
                    continue  # Configuration Server / Client use the DevKey
                try:
                    await self.bind_model(
                        element.address, model_id, app_key_index=app_key_index
                    )
                except (ConfigError, ProxyError) as err:
                    _LOGGER.debug(
                        "skipping SIG model %#06x on %#06x: %s",
                        model_id,
                        element.address,
                        err,
                    )
            for vendor_model in element.vendor_models:
                try:
                    await self.bind_model(
                        element.address, vendor_model, app_key_index=app_key_index
                    )
                except (ConfigError, ProxyError) as err:
                    _LOGGER.debug(
                        "skipping vendor model %s on %#06x: %s",
                        vendor_model,
                        element.address,
                        err,
                    )
