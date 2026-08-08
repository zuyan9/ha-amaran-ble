<p align="center">
  <img src="custom_components/amaran_ble/brand/logo.png" alt="amaran" width="400">
</p>

# amaran BLE for Home Assistant

Local Bluetooth Mesh control for amaran photography lights in Home Assistant.
No amaran account, vendor cloud service, MQTT, or vendor bridge is required.

## Installation

### HACS

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=zuyan9&repository=ha-amaran-ble&category=integration)

1. Open this repository in HACS using the button above. If needed, add
   `https://github.com/zuyan9/ha-amaran-ble` manually as a custom
   **Integration** repository.
2. Install **amaran BLE**.
3. Restart Home Assistant.

## Features

- Power and brightness control, including fixed-daylight fixtures
- Configurable fixture-specific CCT range (the protocol carries 800–20000 K)
- Optional green/magenta tint control
- Hue and saturation control on full-colour fixtures
- Live state updates and automatic reconnection
- Setup and capability options through the Home Assistant UI

## Compatibility

The integration uses the same Telink light-control protocol reverse engineered
by [wesbos/amaran-BLE-control](https://github.com/wesbos/amaran-BLE-control),
without a hard-coded model allowlist. During setup, choose whether the fixture
supports CCT, HSI colour, and green/magenta adjustment; fixed-daylight models
can use a brightness-only profile.

## Requirements

- Home Assistant 2026.8.0 or newer
- A connectable Home Assistant Bluetooth adapter or Bluetooth proxy
- A factory-reset amaran light within Bluetooth range

## Setup

1. Remove the light from Sidus Link, then factory-reset its Bluetooth/Mesh
   settings.
2. Keep the light powered on and close to a connectable Bluetooth adapter.
3. In Home Assistant, open **Settings → Devices & services → Add integration**.
4. Select **amaran BLE**, choose the discovered light, and confirm its
   capabilities.

## Troubleshooting

- **Light not found:** factory-reset Bluetooth/Mesh, close Sidus Link, and move
  the light closer to the Bluetooth adapter.
- **Already provisioned:** the light still belongs to another Mesh network;
  factory-reset it and try again.
- **Entity unavailable:** confirm the light is powered on and that the adapter
  or proxy supports active Bluetooth connections.
- **Removing a light:** keep it powered on and in range while deleting the
  integration so Home Assistant can release it from the private mesh. If that
  cannot complete, factory-reset the fixture before pairing it again.
