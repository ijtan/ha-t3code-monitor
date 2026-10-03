"""Connection health for the T3 Code environment."""

from __future__ import annotations

import hashlib
from typing import Any

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
    coordinator = entry.runtime_data
    entities: list[BinarySensorEntity] = [T3CodeConnectionSensor(coordinator, entry)]
    if coordinator.environment_count > 1:
        entities.extend(
            T3EnvironmentConnectionSensor(coordinator, entry, environment_id)
            for environment_id in coordinator.environment_ids
        )
    async_add_entities(entities)


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

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Identify any selected environments preventing aggregate connectivity."""
        disconnected = [
            {
                "environment_id": environment_id,
                "environment_name": self.coordinator.environment_name(environment_id),
                **self.coordinator.environment_connection_attributes(environment_id),
            }
            for environment_id in self.coordinator.environment_ids
            if not self.coordinator.environment_connected(environment_id)
        ]
        return {
            "configured_environments": self.coordinator.environment_count,
            "connected_environments": (
                self.coordinator.environment_count - len(disconnected)
            ),
            "disconnected_environments": disconnected,
        }


class T3EnvironmentConnectionSensor(
    CoordinatorEntity[T3CodeCoordinator], BinarySensorEntity
):
    """Expose connection health for one selected T3 environment."""

    entity_description = BinarySensorEntityDescription(
        key="environment_connection",
        name="Connection",
        device_class="connectivity",
        entity_category=EntityCategory.DIAGNOSTIC,
    )

    def __init__(
        self,
        coordinator: T3CodeCoordinator,
        entry: ConfigEntry,
        environment_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._environment_id = environment_id
        identity = hashlib.sha256(environment_id.encode()).hexdigest()[:20]
        self._attr_unique_id = f"{entry.entry_id}_environment_{identity}_connection"
        self._attr_device_info = _environment_device_info(
            entry,
            environment_id,
            coordinator.environment_name(environment_id),
        )

    @property
    def available(self) -> bool:
        """Keep per-environment connection state visible during outages."""
        return True

    @property
    def is_on(self) -> bool:
        """Return whether the selected environment can currently be reached."""
        return self.coordinator.environment_connected(self._environment_id)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Show whether health comes from the stream or HTTP snapshots."""
        return self.coordinator.environment_connection_attributes(self._environment_id)


def _environment_device_info(
    entry: ConfigEntry, environment_id: str, environment_name: str
) -> dict[str, Any]:
    """Link one monitored environment device beneath the config-entry device."""
    identity = hashlib.sha256(environment_id.encode()).hexdigest()[:20]
    return {
        "identifiers": {(DOMAIN, f"{entry.entry_id}_environment_{identity}")},
        "name": f"T3 Code - {environment_name}",
        "manufacturer": "T3",
        "model": "T3 Code Environment",
        "via_device": (DOMAIN, entry.entry_id),
    }
