"""Configuration model parsers."""

from __future__ import annotations

import pytest
from amaranble.config_client import CompositionData, ConfigClient, ConfigError
from amaranble.provisioning import Capabilities, ProvisioningError


def test_composition_data_parsing() -> None:
    # Page 0 header plus one element with two SIG models and one vendor model.
    payload = bytes.fromhex(
        "00"  # page
        "1102"  # company 0x0211, little endian
        "0540"  # product 0x4005
        "0100"  # version
        "1000"  # CRPL
        "0000"  # features
        "0000"  # element location
        "0201"  # two SIG, one vendor
        "0010"  # Light Lightness Server 0x1000
        "0013"  # Light CTL Server 0x1300
        "11020000"  # Telink company 0x0211, model 0x0000
    )

    composition = CompositionData.parse(payload, 0x0002)

    assert composition.company_id == 0x0211
    assert composition.product_id == 0x4005
    assert len(composition.elements) == 1
    assert composition.elements[0].address == 0x0002
    assert composition.elements[0].sig_models == [0x1000, 0x1300]
    assert composition.elements[0].vendor_models == [(0x0211, 0x0000)]


def test_composition_data_rejects_truncation() -> None:
    with pytest.raises(ConfigError):
        CompositionData.parse(b"\x00short", 2)

    valid_header = bytes.fromhex("0011020540010010000000")
    with pytest.raises(ConfigError, match="element header"):
        CompositionData.parse(valid_header + b"\x00", 2)


def test_provisioning_capabilities() -> None:
    capabilities = Capabilities.parse(bytes.fromhex("0100010000000000000000"))
    assert capabilities.num_elements == 1
    assert capabilities.supports_cmac_aes128

    with pytest.raises(ProvisioningError, match="must be 11 bytes"):
        Capabilities.parse(b"short")


@pytest.mark.asyncio
async def test_app_key_index_already_stored_is_not_success() -> None:
    class DifferentKeyProxy:
        async def request(self, *_args, **_kwargs):
            class Reply:
                parameters = bytes.fromhex("06000000")

            return Reply()

    config = ConfigClient(DifferentKeyProxy(), 2)  # type: ignore[arg-type]
    with pytest.raises(ConfigError, match="key index already stored"):
        await config.add_app_key(b"A" * 16)
