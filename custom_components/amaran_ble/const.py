"""Constants for the amaran BLE integration."""

from typing import Final

DOMAIN: Final = "amaran_ble"

MANUFACTURER: Final = "amaran"

# The hardware-verified fixture uses this Telink address prefix. It remains
# visible to passive-only Bluetooth proxies when the advertised name does not.
TELINK_ADDRESS_PREFIX: Final = "A4:C1:38"

# Config entry data.
CONF_APP_PRODUCT_ID: Final = "app_product_id"
CONF_NET_KEY: Final = "net_key"
CONF_APP_KEY: Final = "app_key"
CONF_DEVICE_KEY: Final = "device_key"
CONF_UNICAST_ADDRESS: Final = "unicast_address"
CONF_LOCAL_ADDRESS: Final = "local_address"
CONF_IV_INDEX: Final = "iv_index"
CONF_NUM_ELEMENTS: Final = "num_elements"
CONF_INITIAL_SEQUENCE: Final = "initial_sequence"
CONF_SEQUENCE_STORE_ID: Final = "sequence_store_id"
CONF_NEEDS_CONFIGURATION: Final = "needs_configuration"
# The Bluetooth address used to identify the config entry never changes: every
# entity and device-registry identifier is derived from it. A provisioned Mesh
# node may later advertise through a different (for example random) address, so
# keep that reconnect hint separate from the stable identity.
CONF_TRANSPORT_ADDRESS: Final = "transport_address"

# Config entry options.
CONF_MODEL: Final = "model"
CONF_SUPPORTS_CCT: Final = "supports_cct"
CONF_SUPPORTS_COLOR: Final = "supports_color"
CONF_SUPPORTS_GM: Final = "supports_gm"
CONF_MIN_KELVIN: Final = "min_kelvin"
CONF_MAX_KELVIN: Final = "max_kelvin"
CONF_TRANSITION_FADE: Final = "transition_fade"

DEFAULT_SUPPORTS_CCT: Final = True
DEFAULT_SUPPORTS_COLOR: Final = False
DEFAULT_SUPPORTS_GM: Final = False
DEFAULT_MIN_KELVIN: Final = 2700
DEFAULT_MAX_KELVIN: Final = 6500

PROFILE_GENERIC: Final = "generic"
PROFILE_ACE_25X: Final = "ace_25x"
DEFAULT_PROFILE: Final = PROFILE_GENERIC

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
