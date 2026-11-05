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


def _stub_home_assistant() -> None:
    """Provide the few Home Assistant names the coordinators and events import.

    The stubs replace an installed Home Assistant too, so the coordinator
    tests behave the same with or without one.
    """
    from typing import Any, Generic, TypeVar

    data_type = TypeVar("data_type")

    class DataUpdateCoordinator(Generic[data_type]):
        def __init__(self, hass: Any, logger: Any, **kwargs: Any) -> None:
            self.hass = hass
            self.logger = logger
            self.name = kwargs.get("name")
            self.data: Any = None

        def async_set_updated_data(self, data: Any) -> None:
            self.data = data

    class EventEntity:
        async def async_added_to_hass(self) -> None:
            return None

        async def async_will_remove_from_hass(self) -> None:
            return None

        def _trigger_event(
            self, event_type: str, event_attributes: dict[str, Any] | None = None
        ) -> None:
            return None

        def async_write_ha_state(self) -> None:
            return None

    modules = {
        "homeassistant": {},
        "homeassistant.components": {},
        "homeassistant.components.event": {"EventEntity": EventEntity},
        "homeassistant.config_entries": {"ConfigEntry": object},
        "homeassistant.core": {"HomeAssistant": object},
        "homeassistant.helpers": {},
        "homeassistant.helpers.entity_platform": {"AddEntitiesCallback": object},
        "homeassistant.helpers.update_coordinator": {
            "DataUpdateCoordinator": DataUpdateCoordinator
        },
    }
    for module_name, attributes in modules.items():
        module = ModuleType(module_name)
        module.__path__ = []
        module.__dict__.update(attributes)
        sys.modules[module_name] = module


_stub_home_assistant()
