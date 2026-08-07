"""Test bootstrap for the integration's bundled protocol package."""

from __future__ import annotations

import sys
from pathlib import Path

# Import the protocol package without importing Home Assistant. This keeps the
# deterministic codec tests runnable on ordinary Python installations.
sys.path.insert(
    0,
    str(Path(__file__).parents[1] / "custom_components" / "amaran_ble"),
)
