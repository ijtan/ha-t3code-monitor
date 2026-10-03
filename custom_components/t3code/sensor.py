"""Aggregate T3 Code shell counters."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import T3CodeCoordinator
from .const import DOMAIN, SESSION_STATUSES
from .usage_coordinator import T3UsageCoordinator
from .usage_data import UsageLimitWindow, UsagePeriod


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
    if coordinator.environment_count > 1:
        async_add_entities(
            T3EnvironmentSensor(coordinator, entry, environment_id, description)
            for environment_id in coordinator.environment_ids
            for description in DESCRIPTIONS
        )
    usage = coordinator.usage
    async_add_entities(
        T3UsageSensor(usage, entry, period, measure)
        for period in ("month", "90_days")
        for measure in ("tokens", "cost")
    )
    async_add_entities([T3LimitCoverageSensor(usage, entry)])
    if usage.environment_count > 1:
        async_add_entities(
            [
                T3EnvironmentLimitCoverageSensor(usage, entry, environment_id)
                for environment_id in usage.environment_ids
            ]
        )
        async_add_entities(
            T3UsageSensor(usage, entry, period, measure, environment_id)
            for environment_id in usage.environment_ids
            for period in ("month", "90_days")
            for measure in ("tokens", "cost")
        )

    added_limit_keys: set[str] = set()

    def add_new_limit_sensors() -> None:
        new_windows = [
            window
            for key, window in usage.limits.items()
            if key not in added_limit_keys
        ]
        if not new_windows:
            return
        added_limit_keys.update(window.key for window in new_windows)
        async_add_entities(
            T3ProviderLimitSensor(usage, entry, window) for window in new_windows
        )

    add_new_limit_sensors()
    entry.async_on_unload(usage.async_add_listener(add_new_limit_sensors))


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


class T3EnvironmentSensor(CoordinatorEntity[T3CodeCoordinator], SensorEntity):
    """One shell counter for a single selected environment."""

    entity_description: T3CodeSensorDescription

    def __init__(
        self,
        coordinator: T3CodeCoordinator,
        entry: ConfigEntry,
        environment_id: str,
        description: T3CodeSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._environment_id = environment_id
        identity = _environment_identity(environment_id)
        self._attr_name = description.name
        self._attr_unique_id = (
            f"{entry.entry_id}_environment_{identity}_{description.key}"
        )
        self._attr_device_info = _environment_device_info(
            entry,
            environment_id,
            coordinator.environment_name(environment_id),
        )

    @property
    def native_value(self) -> int | None:
        """Return this environment's counter, not the entry-level sum."""
        return self.coordinator.environment_metrics(self._environment_id).get(
            self.entity_description.metric
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the host identity and connection state alongside cached counts."""
        return self.coordinator.environment_connection_attributes(self._environment_id)


class T3UsageSensor(CoordinatorEntity[T3UsageCoordinator], SensorEntity):
    """Usage total or estimated cost for a fixed date window."""

    def __init__(
        self,
        coordinator: T3UsageCoordinator,
        entry: ConfigEntry,
        period: str,
        measure: str,
        environment_id: str | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._period = period
        self._measure = measure
        self._environment_id = environment_id
        period_label = "this month" if period == "month" else "last 90 days"
        measure_label = "tokens" if measure == "tokens" else "estimated API cost"
        self._attr_name = f"Usage {measure_label} {period_label}"
        if environment_id is None:
            self._attr_unique_id = f"{entry.entry_id}_usage_{period}_{measure}"
            self._attr_device_info = _device_info(entry, coordinator.environment_name)
        else:
            identity = _environment_identity(environment_id)
            self._attr_unique_id = (
                f"{entry.entry_id}_environment_{identity}_usage_{period}_{measure}"
            )
            self._attr_device_info = _environment_device_info(
                entry,
                environment_id,
                coordinator.environment_name(environment_id),
            )
        self._attr_native_unit_of_measurement = (
            "tokens" if measure == "tokens" else "USD"
        )
        if measure == "cost":
            self._attr_device_class = SensorDeviceClass.MONETARY
            self._attr_suggested_display_precision = 4
        if measure == "tokens":
            self._attr_icon = "mdi:counter"
        else:
            self._attr_icon = "mdi:cash"

    @property
    def available(self) -> bool:
        """Keep usage sensors unavailable until a valid summary arrives."""
        return super().available and self._period_data is not None

    @property
    def _period_data(self) -> UsagePeriod | None:
        if self._environment_id is None:
            return self.coordinator.periods.get(self._period)
        periods = self.coordinator.periods_for_environment(self._environment_id)
        return None if periods is None else periods.get(self._period)

    @property
    def native_value(self) -> int | float | None:
        """Return total tokens or the API-equivalent cost."""
        period = self._period_data
        if period is None:
            return None
        if self._measure == "tokens":
            return period.total_tokens
        return round(period.cost_usd, 6)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose token breakdown and cost/source coverage transparently."""
        period = self._period_data
        if period is None:
            return None
        return {
            "uncached_input_tokens": period.uncached_input_tokens,
            "cached_input_tokens": period.cached_input_tokens,
            "cache_creation_tokens": period.cache_creation_tokens,
            "output_tokens": period.output_tokens,
            "reasoning_tokens": period.reasoning_tokens,
            "reasoning_tokens_are_subset_of_output": True,
            "cache_savings_usd": round(period.cache_savings_usd, 6),
            "records": period.records,
            "unpriced_records": period.unpriced_records,
            "provider_reported_records": period.provider_reported_records,
            "model_priced_records": period.model_priced_records,
            "source_scanned_files": period.source_scanned_files,
            "source_skipped_files": period.source_skipped_files,
            "source_malformed_records": period.source_malformed_records,
            "source_statuses": period.source_statuses,
            "pricing_statuses": list(period.pricing_statuses),
            "providers": period.providers,
            "summary_errors": (
                self.coordinator.summary_error_for_environment(self._environment_id)
                if self._environment_id is not None
                else self.coordinator.data.summary_errors
            ),
            "summary_updated_at": (
                self.coordinator.summary_updated_at_for_environment(
                    self._environment_id
                )
                if self._environment_id is not None
                else self.coordinator.data.summary_updated_at
            ),
            "environment_id": self._environment_id,
            "environment_name": (
                self.coordinator.environment_name(self._environment_id)
                if self._environment_id is not None
                else None
            ),
            "cost_is_api_equivalent_not_subscription_billing": True,
            "usage_source_scope": (
                "Provider transcript usage on monitored hosts; may include activity outside T3 Code."
            ),
        }


class T3ProviderLimitSensor(CoordinatorEntity[T3UsageCoordinator], SensorEntity):
    """Remaining percentage for one provider-reported quota window."""

    def __init__(
        self,
        coordinator: T3UsageCoordinator,
        entry: ConfigEntry,
        window: UsageLimitWindow,
    ) -> None:
        super().__init__(coordinator)
        key_hash = hashlib.sha256(window.key.encode()).hexdigest()[:20]
        self._window_key = window.key
        self._attr_name = window.name
        self._attr_unique_id = f"{entry.entry_id}_limit_{key_hash}"
        self._attr_native_unit_of_measurement = "%"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:timer-sand"
        self._attr_device_info = (
            _environment_device_info(
                entry,
                window.environment_id,
                window.environment_name,
            )
            if coordinator.environment_count > 1
            else _device_info(entry, coordinator.environment_name)
        )

    @property
    def _window(self) -> UsageLimitWindow | None:
        return self.coordinator.limits.get(self._window_key)

    @property
    def available(self) -> bool:
        """Mark removed or unsupported windows unavailable, not zero remaining."""
        return super().available and self._window is not None

    @property
    def native_value(self) -> float | None:
        """Return remaining quota percentage, as reported by T3's provider probe."""
        window = self._window
        return None if window is None else round(window.remaining_percent, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Include provider usage and reset metadata without exposing account IDs."""
        window = self._window
        if window is None:
            return None
        return {
            "used_percent": round(window.used_percent, 2),
            "window_kind": window.window_kind,
            "window_id": window.window_id,
            "provider": window.provider,
            "environment_id": window.environment_id,
            "environment_name": window.environment_name,
            "account_source": window.account_source,
            "provider_instance_id": window.provider_instance_id,
            "checked_at": window.checked_at,
            "resets_at": window.resets_at,
            "window_duration_minutes": window.window_duration_mins,
            "limit_streams_connected": self.coordinator.data.limit_streams_connected,
        }


class T3LimitCoverageSensor(CoordinatorEntity[T3UsageCoordinator], SensorEntity):
    """Report how many provider quota windows T3 currently exposes."""

    def __init__(self, coordinator: T3UsageCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_name = "Reported provider limit windows"
        self._attr_unique_id = f"{entry.entry_id}_limit_window_count"
        self._attr_icon = "mdi:format-list-numbered"
        self._attr_device_info = _device_info(entry, coordinator.environment_name)

    @property
    def available(self) -> bool:
        """Require a live quota stream before reporting the current window count."""
        return (
            super().available
            and self.coordinator.data is not None
            and self.coordinator.data.limit_streams_connected > 0
        )

    @property
    def native_value(self) -> int:
        """Return the number of quota windows reported by T3."""
        return len(self.coordinator.limits)

    @property
    def extra_state_attributes(self) -> dict[str, int]:
        """Show whether all configured environments are publishing limit data."""
        connected = (
            0
            if self.coordinator.data is None
            else self.coordinator.data.limit_streams_connected
        )
        return {
            "configured_environments": self.coordinator.environment_count,
            "connected_limit_streams": connected,
            "provider_limit_probes_unavailable": (
                0
                if self.coordinator.data is None
                else self.coordinator.data.provider_limit_probes_unavailable
            ),
            "usage_limit_source_errors": (
                0
                if self.coordinator.data is None
                else self.coordinator.data.usage_limit_source_errors
            ),
        }


class T3EnvironmentLimitCoverageSensor(
    CoordinatorEntity[T3UsageCoordinator], SensorEntity
):
    """Report quota-window and stream status for one selected environment."""

    def __init__(
        self,
        coordinator: T3UsageCoordinator,
        entry: ConfigEntry,
        environment_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._environment_id = environment_id
        identity = _environment_identity(environment_id)
        self._attr_name = "Reported provider limit windows"
        self._attr_unique_id = (
            f"{entry.entry_id}_environment_{identity}_limit_window_count"
        )
        self._attr_icon = "mdi:format-list-numbered"
        self._attr_device_info = _environment_device_info(
            entry,
            environment_id,
            coordinator.environment_name(environment_id),
        )

    @property
    def available(self) -> bool:
        """Require this environment's server-config stream to be live."""
        return super().available and self.coordinator.limit_stream_connected(
            self._environment_id
        )

    @property
    def native_value(self) -> int:
        """Return the number of quota windows reported by this host."""
        return len(self.coordinator.limits_for_environment(self._environment_id))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Show this host's quota-source diagnostics and configured identity."""
        return {
            "environment_id": self._environment_id,
            "environment_name": self.coordinator.environment_name(self._environment_id),
            **self.coordinator.limit_diagnostics_for_environment(self._environment_id),
        }


def _device_info(entry: ConfigEntry, name: str) -> dict[str, Any]:
    return {
        "identifiers": {(DOMAIN, entry.entry_id)},
        "name": name,
        "manufacturer": "T3",
        "model": "T3 Code Monitor",
    }


def _environment_identity(environment_id: str) -> str:
    """Return a stable, bounded identifier safe for Home Assistant unique IDs."""
    return hashlib.sha256(environment_id.encode()).hexdigest()[:20]


def _environment_device_info(
    entry: ConfigEntry, environment_id: str, environment_name: str
) -> dict[str, Any]:
    """Link one monitored environment device beneath the config-entry device."""
    identity = _environment_identity(environment_id)
    return {
        "identifiers": {(DOMAIN, f"{entry.entry_id}_environment_{identity}")},
        "name": f"T3 Code - {environment_name}",
        "manufacturer": "T3",
        "model": "T3 Code Environment",
        "via_device": (DOMAIN, entry.entry_id),
    }
