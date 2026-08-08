"""Home Assistant Repairs and in-place reconfiguration coverage."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import pytest
import voluptuous as vol
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from habluetooth import BluetoothServiceInfoBleak
from homeassistant import data_entry_flow
from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.config_entries import SOURCE_RECONFIGURE
from homeassistant.const import CONF_ADDRESS, CONF_NAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.amaran_ble import async_remove_entry, async_setup_entry
from custom_components.amaran_ble.amaranble.gatt import (
    MESH_PROVISIONING_SERVICE,
    MESH_PROXY_SERVICE,
)
from custom_components.amaran_ble.amaranble.network import NetworkKeys
from custom_components.amaran_ble.const import (
    CONF_APP_KEY,
    CONF_DEVICE_KEY,
    CONF_INITIAL_SEQUENCE,
    CONF_IV_INDEX,
    CONF_LOCAL_ADDRESS,
    CONF_MODEL,
    CONF_NEEDS_CONFIGURATION,
    CONF_NET_KEY,
    CONF_NUM_ELEMENTS,
    CONF_SEQUENCE_STORE_ID,
    CONF_TRANSPORT_ADDRESS,
    CONF_UNICAST_ADDRESS,
    DOMAIN,
    PROFILE_GENERIC,
)
from custom_components.amaran_ble.device import AmaranNotProvisionedError
from custom_components.amaran_ble.reconfiguration import (
    async_update_reprovisioned_entry,
)
from custom_components.amaran_ble.repairs import (
    FactoryResetRepairFlow,
    _candidate_title,
    async_create_factory_reset_issue,
    async_create_fix_flow,
    async_delete_factory_reset_issue,
)

ADDRESS = "A4:C1:38:AA:BB:CC"
ALTERNATE_ADDRESS = "A4:C1:38:11:22:33"
ISSUE_ID_PREFIX = "factory_reset_"


def _entry_data(*, transport_address: str | None = None) -> dict[str, object]:
    """Return deterministic persisted credentials for one configured fixture."""
    data: dict[str, object] = {
        CONF_ADDRESS: ADDRESS,
        CONF_NAME: "Stable light",
        CONF_NET_KEY: "01" * 16,
        CONF_APP_KEY: "02" * 16,
        CONF_DEVICE_KEY: "03" * 16,
        CONF_UNICAST_ADDRESS: 2,
        CONF_LOCAL_ADDRESS: 1,
        CONF_NUM_ELEMENTS: 1,
        CONF_IV_INDEX: 0,
        CONF_INITIAL_SEQUENCE: 42,
        CONF_SEQUENCE_STORE_ID: "stable-sequence-store",
    }
    if transport_address is not None:
        data[CONF_TRANSPORT_ADDRESS] = transport_address
    return data


def _replacement_data(address: str = ALTERNATE_ADDRESS) -> dict[str, object]:
    """Return a complete successful replacement provisioning result."""
    return {
        CONF_ADDRESS: address,
        CONF_NAME: "amaran light (112233)",
        CONF_NET_KEY: "01" * 16,
        CONF_APP_KEY: "02" * 16,
        CONF_DEVICE_KEY: "04" * 16,
        CONF_UNICAST_ADDRESS: 2,
        CONF_LOCAL_ADDRESS: 1,
        CONF_NUM_ELEMENTS: 2,
        CONF_IV_INDEX: 0,
        CONF_INITIAL_SEQUENCE: 0,
        CONF_SEQUENCE_STORE_ID: "stable-sequence-store",
        CONF_NEEDS_CONFIGURATION: True,
    }


def _entry(*, transport_address: str | None = None) -> MockConfigEntry:
    """Build an entry whose entity and device identities use the first address."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Studio key light",
        unique_id=ADDRESS,
        data=_entry_data(transport_address=transport_address),
        options={CONF_MODEL: PROFILE_GENERIC},
        minor_version=2,
    )


def _discovery(
    address: str = ALTERNATE_ADDRESS,
    *,
    proxy: bool = False,
    name: str = "amaran test",
) -> BluetoothServiceInfoBleak:
    """Build a connectable reset or authenticated-proxy advertisement."""
    if proxy:
        service_uuid = MESH_PROXY_SERVICE
        service_data = b"\x00" + NetworkKeys.derive(b"\x01" * 16).network_id
    else:
        service_uuid = MESH_PROVISIONING_SERVICE
        service_data = b"\x00" * 16
    return BluetoothServiceInfoBleak(
        name,
        address,
        -45,
        {},
        {service_uuid: service_data},
        [service_uuid],
        "test-scanner",
        BLEDevice(address, name, {}),
        None,
        True,
        1.0,
        None,
    )


def _issue_id(entry: MockConfigEntry) -> str:
    return f"{ISSUE_ID_PREFIX}{entry.entry_id}"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_confirmed_factory_reset_creates_actionable_issue(
    hass: HomeAssistant, issue_registry: ir.IssueRegistry
) -> None:
    """A confirmed membership error enters setup retry and opens a Repair."""
    entry = _entry()
    entry.add_to_hass(hass)
    runtime = Mock()
    runtime.async_start = AsyncMock(side_effect=AmaranNotProvisionedError(ADDRESS))
    runtime.async_stop = AsyncMock()

    with (
        patch("custom_components.amaran_ble.AmaranLight", return_value=runtime),
        pytest.raises(ConfigEntryNotReady, match="repair flow"),
    ):
        await async_setup_entry(hass, entry)

    issue = issue_registry.async_get_issue(DOMAIN, _issue_id(entry))
    assert issue is not None
    assert issue.is_fixable is True
    assert issue.is_persistent is True
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.data == {"entry_id": entry.entry_id}
    runtime.async_stop.assert_awaited_once_with()


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_bare_runtime_setup_keeps_issue_until_authenticated_callback(
    hass: HomeAssistant, issue_registry: ir.IssueRegistry
) -> None:
    """A bare Proxy start cannot clear an issue before authenticated traffic."""
    entry = _entry(transport_address=ALTERNATE_ADDRESS)
    entry.add_to_hass(hass)
    async_create_factory_reset_issue(hass, entry)
    runtime = Mock()
    runtime.async_start = AsyncMock()
    runtime.async_stop = AsyncMock()

    with (
        patch(
            "custom_components.amaran_ble.AmaranLight", return_value=runtime
        ) as light_factory,
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            new=AsyncMock(),
        ),
    ):
        assert await async_setup_entry(hass, entry)

    assert issue_registry.async_get_issue(DOMAIN, _issue_id(entry)) is not None
    kwargs = light_factory.call_args.kwargs
    assert kwargs["transport_address"] == ALTERNATE_ADDRESS

    kwargs["on_provisioned"]()
    assert issue_registry.async_get_issue(DOMAIN, _issue_id(entry)) is None


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_setup_recovers_completed_repair_before_using_old_device_key(
    hass: HomeAssistant, issue_registry: ir.IssueRegistry
) -> None:
    """A crash before Core persisted the entry resumes from durable credentials."""
    entry = _entry()
    entry.add_to_hass(hass)
    async_create_factory_reset_issue(hass, entry)
    replacement = _replacement_data()
    runtime = Mock(async_start=AsyncMock(), async_stop=AsyncMock())

    with (
        patch(
            "custom_components.amaran_ble.async_get_pending",
            new=AsyncMock(return_value={"data": replacement, "committed": True}),
        ),
        patch(
            "custom_components.amaran_ble.async_configure_stored_node",
            new=AsyncMock(return_value=91),
        ) as configure,
        patch(
            "custom_components.amaran_ble.AmaranLight", return_value=runtime
        ) as light_factory,
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            new=AsyncMock(),
        ),
    ):
        assert await async_setup_entry(hass, entry)

    assert configure.await_args.kwargs["device_key"] == b"\x04" * 16
    assert configure.await_args.kwargs["transport_address"] == ALTERNATE_ADDRESS
    assert light_factory.call_args.kwargs["device_key"] == b"\x04" * 16
    assert light_factory.call_args.kwargs["transport_address"] == ALTERNATE_ADDRESS
    assert entry.data[CONF_ADDRESS] == ADDRESS
    assert entry.data[CONF_TRANSPORT_ADDRESS] == ALTERNATE_ADDRESS
    assert entry.data[CONF_DEVICE_KEY] == "04" * 16
    assert entry.data[CONF_INITIAL_SEQUENCE] == 91
    assert CONF_NEEDS_CONFIGURATION not in entry.data
    assert issue_registry.async_get_issue(DOMAIN, _issue_id(entry)) is None


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_setup_preserves_uncertain_repair_and_keeps_issue(
    hass: HomeAssistant, issue_registry: ir.IssueRegistry
) -> None:
    """A pre-DATA record with uncertain completion never runs stale credentials."""
    entry = _entry()
    entry.add_to_hass(hass)
    original = dict(entry.data)

    with (
        patch(
            "custom_components.amaran_ble.async_get_pending",
            new=AsyncMock(
                return_value={"data": _replacement_data(), "committed": False}
            ),
        ),
        pytest.raises(ConfigEntryNotReady, match="interrupted"),
    ):
        await async_setup_entry(hass, entry)

    assert entry.data == original
    assert issue_registry.async_get_issue(DOMAIN, _issue_id(entry)) is not None


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_setup_rejects_pending_credentials_from_another_subnet(
    hass: HomeAssistant,
) -> None:
    """A corrupt or unrelated pending record cannot overwrite an entry."""
    entry = _entry()
    entry.add_to_hass(hass)
    original = dict(entry.data)
    unrelated = {**_replacement_data(), CONF_NET_KEY: "99" * 16}
    runtime = Mock(async_start=AsyncMock(), async_stop=AsyncMock())

    with (
        patch(
            "custom_components.amaran_ble.async_get_pending",
            new=AsyncMock(return_value={"data": unrelated, "committed": True}),
        ),
        patch(
            "custom_components.amaran_ble.AmaranLight", return_value=runtime
        ) as light_factory,
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            new=AsyncMock(),
        ),
    ):
        assert await async_setup_entry(hass, entry)

    assert entry.data == original
    assert light_factory.call_args.kwargs["net_key"] == b"\x01" * 16
    assert light_factory.call_args.kwargs["device_key"] == b"\x03" * 16


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_setup_preserves_committed_repair_with_incomplete_route(
    hass: HomeAssistant, issue_registry: ir.IssueRegistry
) -> None:
    """Incomplete durable data cannot crash setup or reactivate stale keys."""
    entry = _entry()
    entry.add_to_hass(hass)
    original = dict(entry.data)
    incomplete = _replacement_data()
    incomplete.pop(CONF_ADDRESS)

    with (
        patch(
            "custom_components.amaran_ble.async_get_pending",
            new=AsyncMock(return_value={"data": incomplete, "committed": True}),
        ),
        pytest.raises(ConfigEntryNotReady, match="incomplete"),
    ):
        await async_setup_entry(hass, entry)

    assert entry.data == original
    assert issue_registry.async_get_issue(DOMAIN, _issue_id(entry)) is not None


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_reset_during_pending_model_configuration_creates_repair(
    hass: HomeAssistant, issue_registry: ir.IssueRegistry
) -> None:
    """A reset before AppKey binding must not retry forever without a Repair."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Studio key light",
        unique_id=ADDRESS,
        data={**_entry_data(), CONF_NEEDS_CONFIGURATION: True},
        options={CONF_MODEL: PROFILE_GENERIC},
        minor_version=2,
    )
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.amaran_ble.async_get_pending",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "custom_components.amaran_ble.async_configure_stored_node",
            new=AsyncMock(side_effect=AmaranNotProvisionedError(ADDRESS)),
        ),
        patch("custom_components.amaran_ble.AmaranLight") as light_factory,
        pytest.raises(ConfigEntryNotReady, match="repair flow"),
    ):
        await async_setup_entry(hass, entry)

    light_factory.assert_not_called()
    assert issue_registry.async_get_issue(DOMAIN, _issue_id(entry)) is not None


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_removal_clears_issue_and_only_owned_pending_address(
    hass: HomeAssistant, issue_registry: ir.IssueRegistry
) -> None:
    """A transient route cannot delete another fixture's recovery record."""
    entry = _entry(transport_address=ALTERNATE_ADDRESS)
    entry.add_to_hass(hass)
    async_create_factory_reset_issue(hass, entry)

    with (
        patch(
            "custom_components.amaran_ble.async_release_node",
            new=AsyncMock(return_value=True),
        ) as release,
        patch(
            "custom_components.amaran_ble.async_remove_pending", new=AsyncMock()
        ) as remove_pending,
        patch("custom_components.amaran_ble.Store.async_remove", new=AsyncMock()),
    ):
        await async_remove_entry(hass, entry)

    assert issue_registry.async_get_issue(DOMAIN, _issue_id(entry)) is None
    assert release.await_args.kwargs["transport_address"] == ALTERNATE_ADDRESS
    remove_pending.assert_awaited_once_with(hass, ADDRESS)


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_real_reconfigure_flow_preserves_entry_device_and_entities(
    hass: HomeAssistant,
) -> None:
    """HA's reconfigure source atomically updates keys without changing IDs."""
    entry = _entry()
    entry.add_to_hass(hass)
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, ADDRESS)},
        name="Studio key light",
    )
    entity = entity_registry.async_get_or_create(
        Platform.LIGHT,
        DOMAIN,
        ADDRESS,
        config_entry=entry,
        device_id=device.id,
    )
    info = _discovery()
    original_entry_id = entry.entry_id
    original_unique_id = entry.unique_id
    original_options = entry.options

    with (
        patch(
            "custom_components.amaran_ble.reconfiguration.bluetooth.async_discovered_service_info",
            return_value=[info],
        ),
        patch(
            "custom_components.amaran_ble.config_flow.async_reprovision_fixture",
            new=AsyncMock(return_value=_replacement_data()),
        ) as reprovision,
        patch.object(hass.config_entries, "async_schedule_reload") as reload_entry,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": SOURCE_RECONFIGURE,
                "entry_id": entry.entry_id,
            },
        )
        assert result["type"] is data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "reconfigure"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ADDRESS: ALTERNATE_ADDRESS}
        )

    assert result["type"] is data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    reprovision.assert_awaited_once_with(
        hass,
        info,
        recovery_address=ADDRESS,
        prepared_data=_entry_data(),
    )
    reload_entry.assert_called_once_with(entry.entry_id)
    assert entry.entry_id == original_entry_id
    assert entry.unique_id == original_unique_id
    assert entry.options == original_options
    assert entry.data[CONF_ADDRESS] == ADDRESS
    assert entry.data[CONF_TRANSPORT_ADDRESS] == ALTERNATE_ADDRESS
    assert entry.data[CONF_DEVICE_KEY] == "04" * 16
    assert entry.data[CONF_SEQUENCE_STORE_ID] == "stable-sequence-store"
    assert entry.data[CONF_NAME] == "Stable light"
    assert device_registry.async_get(device.id) == device
    assert entity_registry.async_get(entity.entity_id) == entity
    assert entity_registry.async_get(entity.entity_id).unique_id == ADDRESS


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_reconfigure_flow_offers_authenticated_post_complete_proxy(
    hass: HomeAssistant,
) -> None:
    """A rerun can select the Proxy created before a crash updated the entry."""
    entry = _entry()
    entry.add_to_hass(hass)
    info = _discovery(proxy=True)

    with patch(
        "custom_components.amaran_ble.reconfiguration.bluetooth.async_discovered_service_info",
        return_value=[info],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": SOURCE_RECONFIGURE,
                "entry_id": entry.entry_id,
            },
        )

    assert result["type"] is data_entry_flow.FlowResultType.FORM
    assert result["data_schema"]({CONF_ADDRESS: ALTERNATE_ADDRESS}) == {
        CONF_ADDRESS: ALTERNATE_ADDRESS
    }
    hass.config_entries.flow.async_abort(result["flow_id"])


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_reconfigure_flow_offers_non_brand_persisted_reset_route(
    hass: HomeAssistant,
) -> None:
    """A random persisted route remains selectable after another factory reset."""
    persisted_address = "D2:11:22:33:44:55"
    unknown_address = "E2:11:22:33:44:55"
    entry = _entry(transport_address=persisted_address)
    entry.add_to_hass(hass)
    persisted = _discovery(persisted_address, name="Generic Mesh Node")
    unknown = _discovery(unknown_address, name="Generic Mesh Node")

    with patch(
        "custom_components.amaran_ble.reconfiguration.bluetooth.async_discovered_service_info",
        return_value=[persisted, unknown],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": SOURCE_RECONFIGURE,
                "entry_id": entry.entry_id,
            },
        )

    assert result["type"] is data_entry_flow.FlowResultType.FORM
    assert result["data_schema"]({CONF_ADDRESS: persisted_address}) == {
        CONF_ADDRESS: persisted_address
    }
    with pytest.raises(vol.Invalid):
        result["data_schema"]({CONF_ADDRESS: unknown.address})
    hass.config_entries.flow.async_abort(result["flow_id"])


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_repairs_fix_flow_performs_reprovision_and_deletes_issue(
    hass: HomeAssistant, issue_registry: ir.IssueRegistry
) -> None:
    """The Repairs UI performs the fix instead of merely dismissing the issue."""
    entry = _entry()
    entry.add_to_hass(hass)
    async_create_factory_reset_issue(hass, entry)
    assert await async_setup_component(hass, DOMAIN, {})
    assert await async_setup_component(hass, "repairs", {})
    manager = repairs_flow_manager(hass)
    assert manager is not None
    info = _discovery()

    with (
        patch(
            "custom_components.amaran_ble.reconfiguration.bluetooth.async_discovered_service_info",
            return_value=[info],
        ),
        patch(
            "custom_components.amaran_ble.config_flow.async_reprovision_fixture",
            new=AsyncMock(return_value=_replacement_data()),
        ) as reprovision,
        patch.object(hass.config_entries, "async_schedule_reload") as reload_entry,
    ):
        result = await manager.async_init(DOMAIN, data={"issue_id": _issue_id(entry)})
        assert result["type"] is data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "init"
        result = await manager.async_configure(
            result["flow_id"], {CONF_ADDRESS: ALTERNATE_ADDRESS}
        )

    assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY
    reprovision.assert_awaited_once_with(
        hass,
        info,
        recovery_address=ADDRESS,
        prepared_data=_entry_data(),
    )
    reload_entry.assert_called_once_with(entry.entry_id)
    assert entry.data[CONF_ADDRESS] == ADDRESS
    assert entry.data[CONF_TRANSPORT_ADDRESS] == ALTERNATE_ADDRESS
    assert issue_registry.async_get_issue(DOMAIN, _issue_id(entry)) is None


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_repairs_fix_failure_preserves_entry_and_issue(
    hass: HomeAssistant, issue_registry: ir.IssueRegistry
) -> None:
    """Failed live bearer proof is retryable and never overwrites credentials."""
    entry = _entry()
    entry.add_to_hass(hass)
    original_data = dict(entry.data)
    async_create_factory_reset_issue(hass, entry)
    assert await async_setup_component(hass, DOMAIN, {})
    assert await async_setup_component(hass, "repairs", {})
    manager = repairs_flow_manager(hass)
    assert manager is not None
    info = _discovery()

    with (
        patch(
            "custom_components.amaran_ble.reconfiguration.bluetooth.async_discovered_service_info",
            return_value=[info],
        ),
        patch(
            "custom_components.amaran_ble.config_flow.async_reprovision_fixture",
            new=AsyncMock(side_effect=BleakError("ambiguous live bearer")),
        ),
    ):
        result = await manager.async_init(DOMAIN, data={"issue_id": _issue_id(entry)})
        assert result["step_id"] == "init"
        result = await manager.async_configure(
            result["flow_id"], {CONF_ADDRESS: ALTERNATE_ADDRESS}
        )

    assert result["type"] is data_entry_flow.FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    assert entry.data == original_data
    assert issue_registry.async_get_issue(DOMAIN, _issue_id(entry)) is not None


def test_issue_delete_is_idempotent(hass: HomeAssistant) -> None:
    """Cleanup remains safe when HA already removed a resolved issue."""
    async_delete_factory_reset_issue(hass, "missing-entry")
    async_delete_factory_reset_issue(hass, "missing-entry")


async def test_fix_flow_rejects_issue_entry_mismatch(hass: HomeAssistant) -> None:
    """Corrupt issue data cannot redirect a Repair to another config entry."""
    with pytest.raises(ValueError, match="does not match"):
        await async_create_fix_flow(
            hass,
            f"{ISSUE_ID_PREFIX}first-entry",
            {"entry_id": "second-entry"},
        )


def test_reset_candidate_labels_distinguish_stock_names() -> None:
    """Two reset lights with identical BLE names retain address suffixes."""
    first = _discovery("A4:C1:38:11:22:33")
    second = _discovery("A4:C1:38:44:55:66")

    assert _candidate_title(first) == "amaran test (112233)"
    assert _candidate_title(second) == "amaran test (445566)"


async def test_entry_removed_repair_flow_cleans_orphan_issue(
    hass: HomeAssistant, issue_registry: ir.IssueRegistry
) -> None:
    """A stale Repair cannot remain persistent after its entry disappears."""
    entry_id = "removed-entry"
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"{ISSUE_ID_PREFIX}{entry_id}",
        data={"entry_id": entry_id},
        is_fixable=True,
        is_persistent=True,
        severity=ir.IssueSeverity.ERROR,
        translation_key="factory_reset",
        translation_placeholders={"name": "Removed light"},
    )
    flow = FactoryResetRepairFlow(entry_id)
    flow.hass = hass

    result = await flow.async_step_init()

    assert result["type"] is data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "entry_removed"
    assert (
        issue_registry.async_get_issue(DOMAIN, f"{ISSUE_ID_PREFIX}{entry_id}") is None
    )


async def test_loaded_entry_update_listener_prevents_duplicate_reload(
    hass: HomeAssistant,
) -> None:
    """A loaded entry's listener is the sole reload mechanism after repair."""
    entry = _entry()
    entry.add_to_hass(hass)
    listener = AsyncMock()
    entry.add_update_listener(listener)

    with patch.object(hass.config_entries, "async_schedule_reload") as schedule:
        async_update_reprovisioned_entry(hass, entry, _replacement_data())
        await hass.async_block_till_done()

    listener.assert_awaited_once_with(hass, entry)
    schedule.assert_not_called()
