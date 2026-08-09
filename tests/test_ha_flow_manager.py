"""Home Assistant config-flow manager integration tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from bleak.backends.device import BLEDevice
from habluetooth import BluetoothServiceInfoBleak
from homeassistant import data_entry_flow
from homeassistant.config_entries import SOURCE_BLUETOOTH, SOURCE_IGNORE, SOURCE_USER
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.amaran_ble.amaranble.gatt import (
    MESH_PROVISIONING_SERVICE,
    MESH_PROXY_SERVICE,
)
from custom_components.amaran_ble.amaranble.network import NetworkKeys
from custom_components.amaran_ble.config_flow import AmaranConfigFlow
from custom_components.amaran_ble.const import (
    CONF_APP_KEY,
    CONF_DEVICE_KEY,
    CONF_INITIAL_SEQUENCE,
    CONF_IV_INDEX,
    CONF_LOCAL_ADDRESS,
    CONF_MAX_KELVIN,
    CONF_MIN_KELVIN,
    CONF_MODEL,
    CONF_NET_KEY,
    CONF_NUM_ELEMENTS,
    CONF_SEQUENCE_STORE_ID,
    CONF_SUPPORTS_CCT,
    CONF_SUPPORTS_COLOR,
    CONF_SUPPORTS_GM,
    CONF_TRANSPORT_ADDRESS,
    CONF_UNICAST_ADDRESS,
    DOMAIN,
    PROFILE_ACE_25X,
)

ADDRESS = "A4:C1:38:AA:BB:CC"
ROTATED_ADDRESS = "D2:11:22:33:44:55"


def _discovery(address: str = ADDRESS) -> BluetoothServiceInfoBleak:
    """Build a connectable provisioning advertisement."""
    return BluetoothServiceInfoBleak(
        "amaran test",
        address,
        -45,
        {},
        {MESH_PROVISIONING_SERVICE: b"\x00" * 16},
        [MESH_PROVISIONING_SERVICE],
        "test-scanner",
        BLEDevice(address, "amaran test", {}),
        None,
        True,
        1.0,
        None,
    )


def _proxy_discovery(
    address: str = ROTATED_ADDRESS, *, service_data: bytes | None = None
) -> BluetoothServiceInfoBleak:
    """Build a non-branded Proxy page authenticated by the fixture NetKey."""
    if service_data is None:
        service_data = b"\x00" + NetworkKeys.derive(b"\x01" * 16).network_id
    return BluetoothServiceInfoBleak(
        "Generic Mesh Node",
        address,
        -45,
        {},
        {MESH_PROXY_SERVICE: service_data},
        [MESH_PROXY_SERVICE],
        "test-scanner",
        BLEDevice(address, "Generic Mesh Node", {}),
        None,
        True,
        2.0,
        None,
    )


def _provisioned_data(address: str = ADDRESS) -> dict[str, object]:
    """Return deterministic private mesh credentials for flow assertions."""
    return {
        CONF_ADDRESS: address,
        "name": "amaran test (AABBCC)",
        CONF_NET_KEY: "01" * 16,
        CONF_APP_KEY: "02" * 16,
        CONF_DEVICE_KEY: "03" * 16,
        CONF_UNICAST_ADDRESS: 2,
        CONF_LOCAL_ADDRESS: 1,
        CONF_NUM_ELEMENTS: 1,
        CONF_IV_INDEX: 0,
        CONF_INITIAL_SEQUENCE: 0,
        CONF_SEQUENCE_STORE_ID: "flow-sequence-store",
    }


def _ace_options() -> dict[str, object]:
    return {
        CONF_MODEL: PROFILE_ACE_25X,
        CONF_SUPPORTS_CCT: True,
        CONF_SUPPORTS_COLOR: False,
        CONF_SUPPORTS_GM: False,
        CONF_MIN_KELVIN: 2700,
        CONF_MAX_KELVIN: 6500,
    }


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_user_flow_can_reconsider_an_ignored_discovery(
    hass: HomeAssistant,
) -> None:
    """Explicit Add Integration can replace an earlier ignored discovery."""
    ignored = MockConfigEntry(
        domain=DOMAIN,
        title="Ignored light",
        unique_id=ADDRESS,
        source=SOURCE_IGNORE,
    )
    ignored.add_to_hass(hass)
    provision = AsyncMock(return_value=_provisioned_data())

    with (
        patch(
            "custom_components.amaran_ble.config_flow.bluetooth.async_discovered_service_info",
            return_value=[_discovery()],
        ),
        patch.object(AmaranConfigFlow, "_async_provision", provision),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        assert result["type"] is data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "user"
        assert result["data_schema"]({CONF_ADDRESS: ADDRESS}) == {CONF_ADDRESS: ADDRESS}

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ADDRESS: ADDRESS}
        )
        assert result["type"] is data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "confirm"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _ace_options()
        )

    assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == ADDRESS
    assert hass.config_entries.async_get_entry(ignored.entry_id) is None
    provision.assert_awaited_once()


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_bluetooth_flow_provisions_and_creates_entry(
    hass: HomeAssistant,
) -> None:
    """Run discovery and confirmation through HA's real flow manager."""
    info = _discovery()
    provision = AsyncMock(return_value=_provisioned_data())
    with patch.object(AmaranConfigFlow, "_async_provision", provision):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_BLUETOOTH},
            data=info,
        )
        assert result["type"] is data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "confirm"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _ace_options()
        )

    assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"] == _provisioned_data()
    assert result["options"][CONF_MODEL] == PROFILE_ACE_25X
    assert result["result"].unique_id == ADDRESS
    provision.assert_awaited_once_with(info)


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_bluetooth_flow_aborts_an_existing_fixture(
    hass: HomeAssistant,
) -> None:
    """Bluetooth discovery must not offer a second entry for one fixture."""
    MockConfigEntry(
        domain=DOMAIN,
        title="Existing light",
        unique_id=ADDRESS,
        data=_provisioned_data(),
    ).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_BLUETOOTH},
        data=_discovery(),
    )

    assert result["type"] is data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_non_branded_rotated_proxy_recovers_orphaned_pending_entry(
    hass: HomeAssistant,
) -> None:
    """A fresh random address retains the original stable pending identity."""
    info = _proxy_discovery()
    pending_data = _provisioned_data()
    recovered = {
        **pending_data,
        CONF_ADDRESS: ADDRESS,
        CONF_TRANSPORT_ADDRESS: ROTATED_ADDRESS,
    }
    provision = AsyncMock(return_value=recovered)

    with (
        patch(
            "custom_components.amaran_ble.config_flow.async_get_pending_records",
            new=AsyncMock(
                return_value={ADDRESS: {"data": pending_data, "committed": True}}
            ),
        ),
        patch(
            "custom_components.amaran_ble.config_flow.async_provision_fixture",
            new=provision,
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_BLUETOOTH},
            data=info,
        )
        assert result["type"] is data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "confirm"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _ace_options()
        )

    assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == ADDRESS
    assert result["data"][CONF_ADDRESS] == ADDRESS
    assert result["data"][CONF_TRANSPORT_ADDRESS] == ROTATED_ADDRESS
    provision.assert_awaited_once_with(
        hass,
        info,
        _recovery_address=ADDRESS,
        _require_proxy_identity=True,
    )


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_rotated_proxy_owned_by_entry_does_not_start_duplicate_flow(
    hass: HomeAssistant,
) -> None:
    """Cryptographic identity maps a random transport back to its entry."""
    MockConfigEntry(
        domain=DOMAIN,
        title="Existing light",
        unique_id=ADDRESS,
        data=_provisioned_data(),
    ).add_to_hass(hass)

    with patch(
        "custom_components.amaran_ble.config_flow.async_get_pending_records",
        new=AsyncMock(return_value={}),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_BLUETOOTH},
            data=_proxy_discovery(),
        )

    assert result["type"] is data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_corrupt_pending_unicast_cannot_crash_proxy_discovery(
    hass: HomeAssistant,
) -> None:
    """Untrusted durable identity fields are range-checked before AES input."""
    pending_data = {**_provisioned_data(), CONF_UNICAST_ADDRESS: -1}
    with patch(
        "custom_components.amaran_ble.config_flow.async_get_pending_records",
        new=AsyncMock(
            return_value={ADDRESS: {"data": pending_data, "committed": True}}
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_BLUETOOTH},
            data=_proxy_discovery(service_data=b"\x01" + b"\x00" * 16),
        )

    assert result["type"] is data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "not_supported"
