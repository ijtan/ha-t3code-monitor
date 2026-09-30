"""Load the pure projection tests without requiring a Home Assistant install."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).parents[1]
for package_name, package_path in (
    ("custom_components", ROOT / "custom_components"),
    ("custom_components.t3code", ROOT / "custom_components" / "t3code"),
):
    if package_name not in sys.modules:
        package = ModuleType(package_name)
        package.__path__ = [str(package_path)]
        sys.modules[package_name] = package
