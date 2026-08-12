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
from custom_components.amaran_ble.config_flow import (
    AmaranConfigFlow,
    _app_product_id_from_service_data,
    _detected_app_product_id,
    _replacement_model_matches_entry,
    is_amaran_fixture,
)
from custom_components.amaran_ble.const import (
    CONF_APP_KEY,
    CONF_APP_PRODUCT_ID,
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
    PROFILE_GENERIC,
)

ADDRESS = "A4:C1:38:AA:BB:CC"
ROTATED_ADDRESS = "D2:11:22:33:44:55"


def _discovery(
    address: str = ADDRESS,
    *,
    name: str = "amaran test",
    provisioning_data: bytes = b"\x00" * 16,
) -> BluetoothServiceInfoBleak:
    """Build a connectable provisioning advertisement."""
    return BluetoothServiceInfoBleak(
        name,
        address,
        -45,
        {},
        {MESH_PROVISIONING_SERVICE: provisioning_data},
        [MESH_PROVISIONING_SERVICE],
        "test-scanner",
        BLEDevice(address, name, {}),
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


def _ace_selection() -> dict[str, object]:
    return {CONF_MODEL: PROFILE_ACE_25X}


def _generic_capabilities() -> dict[str, object]:
    return {
        CONF_SUPPORTS_CCT: True,
        CONF_SUPPORTS_COLOR: True,
        CONF_SUPPORTS_GM: True,
        CONF_MIN_KELVIN: 2500,
        CONF_MAX_KELVIN: 10000,
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
            result["flow_id"], _ace_selection()
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
            result["flow_id"], _ace_selection()
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
            result["flow_id"], _ace_selection()
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
@pytest.mark.parametrize(
    ("service_data", "expected_product_id"),
    [
        (b"400T5-A1B2C3", "400T5"),
        (b"400t5-A1B2C3", None),
        (b"400T5-abcdef", None),
        (b"FFFFF-A1B2C3", None),
        (b"400T5-\xff1B2C3", None),
        (b"400T5-", None),
    ],
)
def test_app_product_id_parser_is_strict(
    service_data: bytes, expected_product_id: str | None
) -> None:
    """Only complete, ASCII, catalog-recognized provisioning prefixes resolve."""
    assert _app_product_id_from_service_data(service_data) == expected_product_id


@pytest.mark.usefixtures("enable_custom_integrations")
@pytest.mark.parametrize(
    ("info", "expected_product_id"),
    [
        (_discovery(provisioning_data=b"400T5-AABBCC"), "400T5"),
        (
            _discovery(address=ADDRESS.lower(), provisioning_data=b"400T5-AABBCC"),
            "400T5",
        ),
        (_discovery(provisioning_data=b"400T5-112233"), None),
        (_proxy_discovery(), None),
    ],
)
def test_product_id_detection_uses_only_the_provisioning_service(
    info: BluetoothServiceInfoBleak, expected_product_id: str | None
) -> None:
    """A normal Proxy page can never be mistaken for setup model evidence."""
    assert _detected_app_product_id(info) == expected_product_id


@pytest.mark.usefixtures("enable_custom_integrations")
def test_strict_product_id_is_brand_evidence_without_a_telink_address() -> None:
    """A known app identity can safely admit a randomized-address fixture."""
    info = _discovery(
        address="D2:11:22:33:44:55",
        name="SLCK Light",
        provisioning_data=b"400T5-334455",
    )

    assert is_amaran_fixture(info)


@pytest.mark.usefixtures("enable_custom_integrations")
def test_replacement_product_id_must_match_the_configured_fixture() -> None:
    """A repair cannot silently put one model under another model's entities."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_APP_PRODUCT_ID: "400T5"},
        options={CONF_MODEL: PROFILE_ACE_25X},
    )

    assert _replacement_model_matches_entry(
        entry, _discovery(provisioning_data=b"400T5-AABBCC")
    )
    assert not _replacement_model_matches_entry(
        entry, _discovery(provisioning_data=b"400U5-AABBCC")
    )


@pytest.mark.usefixtures("enable_custom_integrations")
@pytest.mark.parametrize(
    ("info", "expected_model"),
    [
        (
            _discovery(name="SLCK Light", provisioning_data=b"400T5-AABBCCtrailing"),
            PROFILE_ACE_25X,
        ),
        (_discovery(name="amaran Ace 25x"), PROFILE_ACE_25X),
        (_discovery(name="amaran-Ace-25x"), PROFILE_GENERIC),
        (_discovery(name="SLCK Light"), PROFILE_GENERIC),
    ],
)
async def test_confirm_prioritizes_strict_id_then_exact_name(
    hass: HomeAssistant,
    info: BluetoothServiceInfoBleak,
    expected_model: str,
) -> None:
    """Strict provisioning evidence wins; an exact name is secondary only."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_BLUETOOTH},
        data=info,
    )

    assert result["type"] is data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "confirm"
    assert result["data_schema"]({}) == {CONF_MODEL: expected_model}


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_detected_model_can_be_overridden_but_evidence_is_persisted(
    hass: HomeAssistant,
) -> None:
    """Detection is a safe default, never authority over the user's choice."""
    info = _discovery(name="SLCK Light", provisioning_data=b"400T5-AABBCCtrailing")
    provision = AsyncMock(return_value=_provisioned_data())
    with patch.object(AmaranConfigFlow, "_async_provision", provision):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_BLUETOOTH},
            data=info,
        )
        assert result["data_schema"]({}) == {CONF_MODEL: PROFILE_ACE_25X}

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_MODEL: "ace_25c"}
        )

    assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["options"][CONF_MODEL] == "ace_25c"
    assert result["data"][CONF_APP_PRODUCT_ID] == "400T5"
    provision.assert_awaited_once_with(info)


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_generic_setup_collects_manual_capabilities_in_a_separate_step(
    hass: HomeAssistant,
) -> None:
    """Named-profile overrides are absent until the explicit Generic path."""
    info = _discovery()
    provision = AsyncMock(return_value=_provisioned_data())
    with patch.object(AmaranConfigFlow, "_async_provision", provision):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_BLUETOOTH},
            data=info,
        )
        assert result["data_schema"]({}) == {CONF_MODEL: PROFILE_GENERIC}

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_MODEL: PROFILE_GENERIC}
        )
        assert result["type"] is data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "generic"
        assert set(result["data_schema"]({})) == set(_generic_capabilities())

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _generic_capabilities()
        )

    assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["options"] == {
        CONF_MODEL: PROFILE_GENERIC,
        **_generic_capabilities(),
    }
    provision.assert_awaited_once_with(info)


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_options_only_show_manual_fields_after_selecting_generic(
    hass: HomeAssistant,
) -> None:
    """Configure follows the same model-first contract as initial setup."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="amaran test",
        data=_provisioned_data(),
        options={CONF_MODEL: PROFILE_ACE_25X},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "init"
    assert result["data_schema"]({}) == {CONF_MODEL: PROFILE_ACE_25X}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_MODEL: PROFILE_GENERIC}
    )
    assert result["type"] is data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "generic"
    assert set(result["data_schema"]({})) == set(_generic_capabilities())

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _generic_capabilities()
    )
    assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_MODEL: PROFILE_GENERIC,
        **_generic_capabilities(),
    }


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
