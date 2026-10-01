"""Push-based T3 Code session and attention events."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import T3CodeCoordinator
from .const import DOMAIN

EVENT_TYPES = (
    "session_created",
    "approval_required",
    "user_input_required",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[T3CodeCoordinator],
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up an event entity for pushed T3 Code state transitions."""
    async_add_entities([T3CodeEventEntity(entry.runtime_data, entry)])


class T3CodeEventEntity(EventEntity):
    """Expose new sessions and attention requests as event types."""

    _attr_event_types: ClassVar[list[str]] = list(EVENT_TYPES)
    _attr_has_entity_name = True

    def __init__(self, coordinator: T3CodeCoordinator, entry: ConfigEntry) -> None:
        self.coordinator = coordinator
        self._attr_name = "Activity"
        self._attr_unique_id = f"{entry.entry_id}_activity"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": coordinator.environment_name,
            "manufacturer": "T3",
            "model": "T3 Code Monitor",
        }
        self._remove_listeners: list[Callable[[], None]] = []

    async def async_added_to_hass(self) -> None:
        """Listen for push events while the entity is loaded."""
        await super().async_added_to_hass()
        self._remove_listeners = [
            self.coordinator.add_event_listener(
                event_type, self._handle_event(event_type)
            )
            for event_type in EVENT_TYPES
        ]

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from coordinator events when unloaded."""
        for remove_listener in self._remove_listeners:
            remove_listener()
        self._remove_listeners.clear()
        await super().async_will_remove_from_hass()

    def _handle_event(self, event_type: str) -> Callable[[dict[str, Any]], None]:
        def trigger(event_data: dict[str, Any]) -> None:
            self._trigger_event(event_type, event_data)

        return trigger
