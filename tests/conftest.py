"""Test bootstrap for the integration's bundled protocol package."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]

# pytest-homeassistant-custom-component does not retain the checkout root on
# sys.path. Add it explicitly so HA can load this repository as a custom
# integration, while retaining the short top-level protocol imports used by
# deterministic codec tests.
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "custom_components" / "amaran_ble"))
