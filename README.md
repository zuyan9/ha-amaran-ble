<p align="center">
  <img src="custom_components/amaran_ble/brand/logo.png" alt="amaran" width="400">
</p>

# amaran BLE for Home Assistant

Local Bluetooth Mesh control for amaran photography lights in Home Assistant.
No amaran account, cloud service, MQTT, or external bridge is required.

## Features

- Power and brightness control
- Adjustable colour temperature
- Hue and saturation control on full-colour fixtures
- Live state updates and automatic reconnection
- Setup and capability options through the Home Assistant UI

The **amaran Ace 25x** is hardware-tested for power, brightness, and its
2700–6500 K colour-temperature range. Other models may work but have not yet
been verified, and RGB control has not yet been tested on physical hardware.
Effects, fan control, and boost mode are not currently exposed.

## Requirements

- Home Assistant 2026.8.0 or newer
- A connectable Home Assistant Bluetooth adapter or Bluetooth proxy
- A factory-reset amaran light within Bluetooth range

## Installation

### HACS

1. Add `https://github.com/zuyan9/ha-amaran-ble` to HACS as a custom
   **Integration** repository.
2. Install **amaran BLE**.
3. Restart Home Assistant.

### Manual

Copy `custom_components/amaran_ble` into your Home Assistant
`custom_components` directory, then restart Home Assistant.

## Setup

> [!IMPORTANT]
> Home Assistant provisions the light into its own private Bluetooth Mesh
> network. Sidus Link cannot control the light afterward unless you remove it
> from Home Assistant or factory-reset its Bluetooth/Mesh settings.

1. Remove the light from Sidus Link, then factory-reset its Bluetooth/Mesh
   settings.
2. Keep the light powered on and close to a connectable Bluetooth adapter.
3. In Home Assistant, open **Settings → Devices & services → Add integration**.
4. Select **amaran BLE**, choose the discovered light, and confirm its
   capabilities.

Model names ending in `x`, such as the Ace 25x, are generally bi-colour. Model
names ending in `c` are generally full-colour; enable the full-colour option for
those fixtures. Set the colour-temperature range to the values specified for
your model.

Deleting the integration sends a best-effort Mesh reset to release the light.
If the light is powered off or unreachable during removal, factory-reset it
manually before pairing it elsewhere.

## Troubleshooting

- **Light not found:** factory-reset Bluetooth/Mesh, close Sidus Link, and move
  the light closer to the Bluetooth adapter.
- **Already provisioned:** the light still belongs to another Mesh network;
  factory-reset it and try again.
- **Entity unavailable:** confirm the light is powered on and that the adapter
  or proxy supports active Bluetooth connections.

Please report unsupported models or problems through
[GitHub Issues](https://github.com/zuyan9/ha-amaran-ble/issues).

## Credits

The light-control protocol is based on the reverse engineering in
[wesbos/amaran-BLE-control](https://github.com/wesbos/amaran-BLE-control). See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for its retained attribution.

This is an unofficial project and is not affiliated with amaran, Aputure, or
Sidus Link.
