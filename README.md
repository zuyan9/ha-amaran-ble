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

- Fully local power, brightness, CCT, HSI colour, and optional green/magenta
  control
- Live state updates, automatic reconnection, and crash-safe Bluetooth Mesh
  sequence handling
- A dedicated **Ace 25x** profile with:
  - All nine built-in effects, effect intensity, rate/Random, CCT, and colour
    presets
  - Boost mode and its 3800–5500 K colour-temperature control
  - Report-confirmed fan modes, fan speed, and internal temperature
  - Battery level, estimated runtime, power source, and firmware diagnostics
- Setup and model/capability options through the Home Assistant UI

Some effects flash rapidly. Use Strobe, Lightning, Explosion, Paparazzi, and
Fireworks with appropriate photosensitivity precautions.

## Compatibility

The **Ace 25x** is the hardware-tested model. Select its named profile to enable
the model-specific controls above.

Other single-zone Telink-based amaran/Aputure fixtures may work using the
**Generic** profile. Their power, brightness, CCT, HSI, and G/M capabilities are
configured manually and remain experimental. Multi-zone/pixel effects, groups,
scenes, app shortcuts, USB output, physical-button settings, factory reset,
and firmware updates are not currently exposed.

The light-control foundation builds on the protocol reverse engineered by
[wesbos/amaran-BLE-control](https://github.com/wesbos/amaran-BLE-control).

## Requirements

- Home Assistant 2026.8.0 or newer
- A connectable Home Assistant Bluetooth adapter or Bluetooth proxy
- A factory-reset amaran light within Bluetooth range

## Setup

1. Remove the light from Sidus Link, then factory-reset its Bluetooth/Mesh
   settings.
2. Keep the light powered on and close to a connectable Bluetooth adapter.
3. In Home Assistant, open **Settings → Devices & services → Add integration**.
4. Select **amaran BLE**, choose the discovered light, and select its model.

For an existing installation, open **Settings → Devices & services → amaran
BLE → Configure**, select **amaran Ace 25x**, and save to add its extended
entities.

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
