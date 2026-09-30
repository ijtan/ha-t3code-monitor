"""Aggregate T3 Code shell counters."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import T3CodeCoordinator
from .const import DOMAIN, SESSION_STATUSES


@dataclass(frozen=True, kw_only=True)
class T3CodeSensorDescription(SensorEntityDescription):
    """Describe a derived shell counter."""

    metric: str


DESCRIPTIONS = [
    *[
        T3CodeSensorDescription(
            key=f"session_{status}",
            name=f"Sessions {status}",
            icon="mdi:robot",
            metric=f"session_{status}",
        )
        for status in SESSION_STATUSES
    ],
    T3CodeSensorDescription(
        key="session_missing",
        name="Threads without session",
        icon="mdi:robot-off",
        metric="session_missing",
    ),
    T3CodeSensorDescription(
        key="pending_approvals",
        name="Pending approvals",
        icon="mdi:shield-alert",
        metric="pending_approvals",
    ),
    T3CodeSensorDescription(
        key="pending_input",
        name="Pending input",
        icon="mdi:message-question",
        metric="pending_input",
    ),
    T3CodeSensorDescription(
        key="running_turns",
        name="Running turns",
        icon="mdi:progress-clock",
        metric="running_turns",
    ),
    T3CodeSensorDescription(
        key="threads", name="Visible threads", icon="mdi:forum", metric="threads"
    ),
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[T3CodeCoordinator],
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up aggregate sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        T3CodeSensor(coordinator, entry, description) for description in DESCRIPTIONS
    )


class T3CodeSensor(CoordinatorEntity[T3CodeCoordinator], SensorEntity):
    """One aggregate count from the environment's current shell projection."""

    entity_description: T3CodeSensorDescription

    def __init__(
        self,
        coordinator: T3CodeCoordinator,
        entry: ConfigEntry,
        description: T3CodeSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": coordinator.environment_name,
            "manufacturer": "T3",
            "model": "T3 Code Monitor",
        }

    @property
    def native_value(self) -> int | None:
        """Return the aggregate count."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get(self.entity_description.metric)
