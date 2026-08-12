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

- Named profiles for 77 amaran and Aputure models represented by 80 product IDs
  in the app's bundled fixture catalog
- Fully local power, brightness, model-specific CCT ranges, HSI colour, and
  green/magenta control where supported
- Cataloged legacy effects and defaults-proven generation-II effects with
  selected rate and colour parameters, including CCT/HSI effect switching,
  plus seven multi-zone pixel effects
- Profile-gated Boost, reported fan modes and manual fan speed, power/runtime,
  firmware diagnostics, and high-speed-photography controls
- Live state updates, cryptographically verified reconnection across Bluetooth
  address changes, and crash-safe provisioning and Mesh sequence handling
- Home Assistant Repairs support to re-provision a factory-reset fixture in
  place without replacing its device or entity IDs
- Setup and model/capability options through the Home Assistant UI

Some effects flash rapidly. Use Strobe, Lightning, Explosion, Paparazzi, and
Fireworks with appropriate photosensitivity precautions.

## Compatibility

The **Ace 25x** is the only hardware-tested model. Every other named model is an
experimental best-effort profile derived from the official app's bundled
capability data and reverse-engineered protocol code. Select the exact model;
use **Generic** only when it is not listed.

### Model catalog provenance

The static model profiles are pinned to `fixtureConfig.json` extracted from the
official [amaran Android app](https://play.google.com/store/apps/details?id=com.sidus.link.amaran),
version 1.0.71 (version code 32), whose bundled catalog is revision 30. The
audited file's SHA-256 is
`3493e7aa34661bf18efe06d3a07897bdf58b785e8087e658520f62c8d52836a0`.
This makes model-parameter changes reviewable instead of silently following a
new mobile-app release. The vendor's live catalog was revision 41 during the
2026-08-11 audit; its Ace 25x record was unchanged from revision 30. The newer
catalog was inspected but is not silently substituted for the pinned snapshot.

The [iOS App Store listing](https://apps.apple.com/us/app/amaran/id6503329243)
reported version 1.0.70 during the 2026-08-11 audit, but its bundled catalog was
not independently extracted and compared. The integration therefore does not
claim that the iOS and Android catalog files are identical.

A factory-reset fixture can expose the app product ID in the first 12 ASCII
bytes of its Mesh Provisioning service data (for example, `400T5-A1B2C3` for
an Ace 25x). Home Assistant preselects a model only when that ID strictly
matches the pinned catalog, and still asks the user to confirm or override it.
Normal post-provisioning Mesh reports do not carry this ID. Named models use the
pinned catalog profile; only **Generic** opens a second step for manual CCT,
colour, G/M, and temperature-range settings.

The integration currently exposes capabilities whose packet layout, range, and
safe defaults are known. It also includes tested low-level codecs for additional
app protocol families—including RGBW, Gel, XY, generation-III effects,
partitions, Magic Pixel, motion, and manual effects—but does not present those
as Home Assistant controls where the app did not reveal safe units, defaults,
or state transitions. Program/Picker/Music/Touchbar effects, groups, scenes,
Quickshots, OTA updates, USB output, and physical-button settings are not yet
user-facing.

Some models may use a different or relay-only Mesh proxy path. In particular,
the upstream project controls the Halo 100x through another fixture, so direct
Halo provisioning remains especially uncertain.

The light-control foundation builds on the protocol reverse engineered by
[wesbos/amaran-BLE-control](https://github.com/wesbos/amaran-BLE-control).

## Requirements

- Home Assistant 2026.8.0 or newer
- A connectable Home Assistant Bluetooth adapter or Bluetooth proxy
- A factory-reset supported amaran/Aputure fixture within Bluetooth range

## Setup

1. Before removing the fixture from the amaran app, record its exact **Product
   model**. In the audited Android app this is under **Device → fixture → gear
   → Product model**. The public iOS UI shows the same fixture-settings route,
   but its settings contents and bundled model catalog have not been
   independently extracted.
2. Remove the fixture from the amaran app, then factory-reset its
   Bluetooth/Mesh settings.
3. Keep the fixture powered on and close to a connectable Bluetooth adapter.
4. In Home Assistant, open **Settings → Devices & services → Add integration**.
5. Select **amaran BLE**, choose the discovered light, and confirm its model.
   When the strict app product ID is present, Home Assistant preselects it.

For an existing installation, open **Settings → Devices & services → amaran
BLE → Configure**, select the exact fixture model, and save to reload its
profile-specific entities.

## Troubleshooting

- **Light not found:** factory-reset Bluetooth/Mesh, close Sidus Link, and move
  the light closer to the Bluetooth adapter.
- **Already provisioned:** the light still belongs to another Mesh network;
  factory-reset it and try again.
- **Entity unavailable:** confirm the light is powered on and that the adapter
  or proxy supports active Bluetooth connections.
- **Fixture was factory reset:** open the Home Assistant Repair shown for the
  integration and select the reset fixture. Home Assistant re-provisions it in
  place, preserving the existing device and entity IDs.
- **Removing a light:** keep it powered on and in range while deleting the
  integration so Home Assistant can release it from the private mesh. If that
  cannot complete, factory-reset the fixture before pairing it again.
- **Reporting experimental hardware issues:** download the integration's
  diagnostics and use the repository's hardware bug-report form. Inspect every
  attachment and redact full MAC addresses and Mesh keys before posting it.

## Development

The locked test environment includes the integration's minimum supported Home
Assistant release, so Home Assistant-facing tests run locally instead of being
silently skipped:

```console
uv sync --locked
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```
