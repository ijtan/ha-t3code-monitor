"""Connection health for the T3 Code environment."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import T3CodeCoordinator
from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[T3CodeCoordinator],
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the connection health entity."""
    async_add_entities([T3CodeConnectionSensor(entry.runtime_data, entry)])


class T3CodeConnectionSensor(CoordinatorEntity[T3CodeCoordinator], BinarySensorEntity):
    """Expose stream connectivity while remaining available during outages."""

    entity_description = BinarySensorEntityDescription(
        key="connection",
        name="Connection",
        device_class="connectivity",
        entity_category=EntityCategory.DIAGNOSTIC,
    )

    def __init__(self, coordinator: T3CodeCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_connection"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": coordinator.environment_name,
            "manufacturer": "T3",
            "model": "T3 Code Monitor",
        }

    @property
    def available(self) -> bool:
        """Keep this diagnostic entity visible during a disconnect."""
        return True

    @property
    def is_on(self) -> bool:
        """Return whether all selected environment connections are healthy."""
        return self.coordinator.all_connected
