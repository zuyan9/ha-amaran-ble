# amaran BLE for Home Assistant

Local Bluetooth control for amaran and Aputure photography lights. The
integration provisions each fixture into a small Home Assistant-owned
Bluetooth Mesh network and exposes it as a native `light` entity.

The implementation is unofficial and does not use an amaran account, Sidus
Link, the cloud, MQTT, or an external bridge.

## Supported features

- Power
- Brightness (0–100%)
- Correlated colour temperature
- HS colour on RGB fixtures
- Pushed state updates plus periodic refresh
- Automatic reconnect through Home Assistant Bluetooth adapters and proxies
- UI-based discovery, setup, and capability options

The amaran Ace 25x is hardware-tested for power, brightness, and its official
2700–6500 K CCT range. It is bi-colour, so turn off **Fixture supports full
colour** during setup.

Effects, fan control, and boost mode are not exposed yet. The Ace advertises
those capabilities, but their `0x26` payloads have not been captured and
validated safely.

## Important: fixture ownership

A Bluetooth Mesh fixture can only belong to one mesh network at a time. This
integration deliberately creates and owns a private network for each light.

Before adding a fixture:

1. Remove it from the amaran/Sidus Link app if present.
2. Factory-reset its Bluetooth/Mesh settings using the fixture's menu.
3. Keep it powered on and near a connectable Home Assistant Bluetooth adapter.
4. In Home Assistant, open **Settings → Devices & services → Add integration**
   and choose **amaran BLE**.

After Home Assistant provisions the light, the vendor app cannot control it.
Deleting the Home Assistant config entry makes a best-effort Mesh Node Reset so
the fixture can be adopted again. If the fixture is off or out of range during
deletion, factory-reset it manually before pairing elsewhere.

## Installation

### HACS custom repository

1. Open HACS and choose **Integrations**.
2. Add this repository as a custom repository of type **Integration**.
3. Install **amaran BLE**.
4. Restart Home Assistant.

### Manual

Copy `custom_components/amaran_ble` into the same directory under your Home
Assistant configuration folder, then restart Home Assistant:

```text
config/
└── custom_components/
    └── amaran_ble/
        ├── __init__.py
        ├── config_flow.py
        ├── light.py
        └── manifest.json
```

Home Assistant Core 2026.8.0 is the currently tested release.

## Bluetooth discovery

Factory-reset fixtures advertise the standard Mesh Provisioning service
`0x1827`. Provisioned fixtures advertise Mesh Proxy service `0x1828`. The
integration additionally requires amaran's observed Telink manufacturer ID or
`SLCK` local name so it does not offer unrelated Bluetooth Mesh products.

If a fixture is not listed:

- confirm it is factory-reset rather than merely powered off;
- close the amaran/Sidus Link app so it is not holding the BLE connection;
- use a Home Assistant Bluetooth adapter or proxy that supports active
  connections;
- move the light closer for initial provisioning;
- reload the Bluetooth integration, then try **Add integration** again.

## How it works

The integration implements the relevant Bluetooth Mesh layers directly:

- PB-GATT no-OOB provisioning over service `0x1827`;
- NetKey/AppKey/DeviceKey derivation and AES-CMAC/AES-CCM framing;
- Mesh Proxy SAR, proxy filters, replay-safe sequence numbers, and segmented
  access messages over service `0x1828`;
- configuration-model AppKey installation and model binding;
- amaran's proprietary access opcode `0x26` for physical LED control.

Sequence numbers are reserved in durable blocks before transmission. This is
important: reusing a number after an abrupt Home Assistant restart causes the
fixture to reject otherwise valid packets as replays.

## Development

The deterministic protocol suite can run without a Home Assistant checkout:

```bash
uv run --no-project --python 3.11 \
  --with pytest --with pytest-asyncio --with cryptography \
  pytest -q
```

The integration itself targets the Python and APIs bundled with Home Assistant
2026.8.0. Before release, also run Home Assistant's configuration check against
an installation containing the component.

## Protocol acknowledgements

The proprietary light payload encoder and status decoder are based on the
reverse engineering in [wesbos/amaran-BLE-control](https://github.com/wesbos/amaran-BLE-control).
Its MIT attribution is retained in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
The Home Assistant lifecycle and BLE UX were informed by
[rabits/ha-ef-ble](https://github.com/rabits/ha-ef-ble) and current Home
Assistant Bluetooth integration guidance.

This project is not affiliated with amaran, Aputure, or Sidus Link.
