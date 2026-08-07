"""Constants for the amaran BLE integration."""

from typing import Final

DOMAIN: Final = "amaran_ble"

MANUFACTURER: Final = "amaran"

# Telink Semiconductor, the SoC vendor amaran fixtures use. The company ID
# only appears in the scan response, which passive-only scanners never see;
# the address prefix is in every advertisement.
TELINK_COMPANY_ID: Final = 0x0211
TELINK_ADDRESS_PREFIX: Final = "A4:C1:38"

# Config entry data.
CONF_NET_KEY: Final = "net_key"
CONF_APP_KEY: Final = "app_key"
CONF_DEVICE_KEY: Final = "device_key"
CONF_UNICAST_ADDRESS: Final = "unicast_address"
CONF_LOCAL_ADDRESS: Final = "local_address"
CONF_IV_INDEX: Final = "iv_index"
CONF_NUM_ELEMENTS: Final = "num_elements"
CONF_MODEL: Final = "model"
CONF_INITIAL_SEQUENCE: Final = "initial_sequence"
CONF_NEEDS_CONFIGURATION: Final = "needs_configuration"

# Config entry options.
CONF_SUPPORTS_COLOR: Final = "supports_color"
CONF_MIN_KELVIN: Final = "min_kelvin"
CONF_MAX_KELVIN: Final = "max_kelvin"
CONF_TRANSITION_FADE: Final = "transition_fade"

DEFAULT_SUPPORTS_COLOR: Final = False
DEFAULT_MIN_KELVIN: Final = 2700
DEFAULT_MAX_KELVIN: Final = 6500

# The provisioner always occupies 0x0001 and the fixture 0x0002: each config
# entry owns a private single-node mesh, so the addresses never collide.
PROVISIONER_ADDRESS: Final = 0x0001
NODE_ADDRESS: Final = 0x0002

# How far the sequence number is advanced past the last persisted checkpoint on
# start-up, and how often a new checkpoint is written. A node silently drops
# messages whose sequence number it has already seen, so reuse after a restart
# would look like the light ignoring commands.
SEQUENCE_CHECKPOINT: Final = 512

# Keep-alive / state refresh interval, in seconds.
POLL_INTERVAL: Final = 60

# Used instead while the fixture has never reported: the entity is
# unavailable until the first report arrives, so a full interval of waiting
# is very visible right after a restart.
INITIAL_POLL_INTERVAL: Final = 5

RECONNECT_MIN_DELAY: Final = 5
RECONNECT_MAX_DELAY: Final = 300
