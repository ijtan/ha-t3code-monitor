"""Fetch read-only usage history and provider quota snapshots from T3."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN
from .errors import T3ClientError
from .t3_client import T3Client
from .usage_data import (
    UsageCoordinatorData,
    UsageLimitWindow,
    UsagePeriod,
    aggregate_usage_by_environment,
    aggregate_usage_periods,
    provider_limit_windows,
    source_limit_windows,
    unavailable_provider_limit_probes,
    usage_limit_source_errors,
)

_LOGGER = logging.getLogger(__name__)
USAGE_REFRESH_INTERVAL = timedelta(minutes=30)
LIMIT_RECONNECT_DELAY = 15


class T3UsageCoordinator(DataUpdateCoordinator[UsageCoordinatorData]):
    """Refresh 90-day usage summaries and stream provider quota updates."""

    def __init__(
        self,
        hass: HomeAssistant,
        clients: dict[str, tuple[str, T3Client]],
        name: str,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{name}_usage",
            update_interval=None,
        )
        self.environment_name = name
        self.entry = entry
        self.entry_id = entry.entry_id
        self._clients = clients
        self._summaries: dict[str, dict[str, Any]] = {}
        self._summary_errors: set[str] = set()
        self._provider_windows: dict[str, dict[str, UsageLimitWindow]] = {}
        self._source_windows: dict[str, dict[str, UsageLimitWindow]] = {}
        self._provider_unavailable: dict[str, int] = {}
        self._source_errors: dict[str, int] = {}
        self._connected_limit_streams: set[str] = set()
        self._limit_failures_logged: set[str] = set()
        self._tasks: list[asyncio.Task[None]] = []
        self._last_summary_update: str | None = None

    @property
    def periods(self) -> dict[str, UsagePeriod | None]:
        """Latest current-month and rolling-90-day totals."""
        if self.data is None:
            return {"month": None, "90_days": None}
        return self.data.periods

    @property
    def limits(self) -> dict[str, UsageLimitWindow]:
        """Latest available provider-reported quota windows."""
        return {} if self.data is None else self.data.limits

    @property
    def environment_count(self) -> int:
        """Number of configured environments included in usage polling."""
        return len(self._clients)

    @property
    def environment_ids(self) -> tuple[str, ...]:
        """Configured environment IDs, in their setup order."""
        return tuple(self._clients)

    def environment_name(self, environment_id: str) -> str:
        """Return the display name associated with one configured environment."""
        environment = self._clients.get(environment_id)
        return environment[0] if environment is not None else environment_id

    def periods_for_environment(
        self, environment_id: str
    ) -> dict[str, UsagePeriod] | None:
        """Return the current-month and rolling-90-day periods for one host."""
        if self.data is None:
            return None
        return self.data.environment_periods.get(environment_id)

    def summary_error_for_environment(self, environment_id: str) -> bool:
        """Whether the latest usage-summary request for one host failed."""
        return (
            self.data.environment_summary_errors.get(environment_id, False)
            if self.data is not None
            else False
        )

    def summary_updated_at_for_environment(self, environment_id: str) -> str | None:
        """Return the latest successful usage-scan time for one host."""
        return (
            self.data.environment_summary_updated_at.get(environment_id)
            if self.data is not None
            else None
        )

    def limits_for_environment(
        self, environment_id: str
    ) -> dict[str, UsageLimitWindow]:
        """Return quota windows reported by one host only."""
        return {
            **self._provider_windows.get(environment_id, {}),
            **self._source_windows.get(environment_id, {}),
        }

    def limit_stream_connected(self, environment_id: str) -> bool:
        """Whether one host's provider-limit stream has delivered a snapshot."""
        return environment_id in self._connected_limit_streams

    def limit_diagnostics_for_environment(self, environment_id: str) -> dict[str, int]:
        """Return provider-probe and usage-source errors for one host."""
        return {
            "provider_limit_probes_unavailable": self._provider_unavailable.get(
                environment_id, 0
            ),
            "usage_limit_source_errors": self._source_errors.get(environment_id, 0),
        }

    def start(self) -> None:
        """Start usage refresh and one provider-limit stream per environment."""
        self._tasks.append(
            self.entry.async_create_background_task(
                self.hass,
                self._usage_refresh_loop(),
                f"t3code_{self.entry_id}_usage_refresh",
            )
        )
        for environment_id, (environment_name, client) in self._clients.items():
            self._tasks.append(
                self.entry.async_create_background_task(
                    self.hass,
                    self._limit_stream_loop(environment_id, environment_name, client),
                    f"t3code_{self.entry_id}_{environment_id}_limits",
                )
            )

    async def stop(self) -> None:
        """Cancel usage refresh and quota subscriptions."""
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def async_refresh_usage(self) -> None:
        """Fetch 90 days once, then derive month-to-date and rolling totals."""
        zone = _time_zone(self.hass.config.time_zone)
        today = datetime.now(zone).date()
        window = {
            "sinceDay": (today - timedelta(days=89)).isoformat(),
            "untilDay": today.isoformat(),
            "timeZone": self.hass.config.time_zone or "UTC",
            "resolution": "day",
        }
        results = await asyncio.gather(
            *(
                client.get_usage_summary(window)
                for _environment_id, (_name, client) in self._clients.items()
            ),
            return_exceptions=True,
        )
        successful_read = False
        for (environment_id, (_environment_name, _client)), result in zip(
            self._clients.items(), results, strict=True
        ):
            if isinstance(result, BaseException):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                self._summary_errors.add(environment_id)
                _LOGGER.warning(
                    "T3 Code usage summary unavailable for environment %s (%s): %s",
                    _environment_name,
                    environment_id,
                    result
                    if isinstance(result, T3ClientError)
                    else type(result).__name__,
                )
                continue
            self._summaries[environment_id] = result
            self._summary_errors.discard(environment_id)
            successful_read = True
        if successful_read:
            self._last_summary_update = datetime.now(timezone.utc).isoformat()
        self._publish(today)

    async def _usage_refresh_loop(self) -> None:
        while True:
            try:
                await self.async_refresh_usage()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - keep refresh loop alive
                _LOGGER.error(
                    "Unexpected error refreshing T3 Code usage summaries, error type %s",
                    type(err).__name__,
                )
            await asyncio.sleep(USAGE_REFRESH_INTERVAL.total_seconds())

    async def _limit_stream_loop(
        self, environment_id: str, environment_name: str, client: T3Client
    ) -> None:
        while True:
            try:
                async for events in client.subscribe_server_config():
                    self._connected_limit_streams.add(environment_id)
                    if environment_id in self._limit_failures_logged:
                        _LOGGER.info(
                            "T3 Code provider-limit stream reconnected for environment %s (%s)",
                            environment_name,
                            environment_id,
                        )
                        self._limit_failures_logged.discard(environment_id)
                    for event in events:
                        self._handle_config_event(
                            environment_id, environment_name, event
                        )
                    self._publish()
            except asyncio.CancelledError:
                raise
            except T3ClientError as err:
                if environment_id not in self._limit_failures_logged:
                    _LOGGER.warning(
                        "T3 Code provider-limit stream unavailable for environment %s (%s): %s",
                        environment_name,
                        environment_id,
                        err,
                    )
                    self._limit_failures_logged.add(environment_id)
                else:
                    _LOGGER.debug(
                        "T3 Code provider-limit stream retry failed for environment %s (%s): %s",
                        environment_name,
                        environment_id,
                        err,
                    )
            except Exception as err:  # noqa: BLE001 - keep subscription loop alive
                log = (
                    _LOGGER.debug
                    if environment_id in self._limit_failures_logged
                    else _LOGGER.error
                )
                log(
                    "Unexpected error reading T3 Code provider limits for environment %s (%s), error type %s",
                    environment_name,
                    environment_id,
                    type(err).__name__,
                )
                self._limit_failures_logged.add(environment_id)
            self._connected_limit_streams.discard(environment_id)
            self._publish()
            await asyncio.sleep(LIMIT_RECONNECT_DELAY)

    def _handle_config_event(
        self, environment_id: str, environment_name: str, event: dict[str, Any]
    ) -> None:
        event_type = event.get("type")
        if event_type == "snapshot":
            config = event.get("config")
            if isinstance(config, dict):
                providers = config.get("providers")
                self._provider_windows[environment_id] = provider_limit_windows(
                    environment_id, environment_name, providers
                )
                self._provider_unavailable[environment_id] = (
                    unavailable_provider_limit_probes(providers)
                )
                sources = config.get("usageLimitSources")
                if isinstance(sources, list):
                    self._source_windows[environment_id] = source_limit_windows(
                        environment_id, environment_name, sources
                    )
                    self._source_errors[environment_id] = usage_limit_source_errors(
                        sources
                    )
            return
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return
        if event_type == "providerStatuses":
            providers = payload.get("providers")
            self._provider_windows[environment_id] = provider_limit_windows(
                environment_id, environment_name, providers
            )
            self._provider_unavailable[environment_id] = (
                unavailable_provider_limit_probes(providers)
            )
        elif event_type == "usageLimitSourcesUpdated":
            sources = payload.get("sources")
            self._source_windows[environment_id] = source_limit_windows(
                environment_id, environment_name, sources
            )
            self._source_errors[environment_id] = usage_limit_source_errors(sources)

    def _publish(self, today: date | None = None) -> None:
        zone = _time_zone(self.hass.config.time_zone)
        current_day = today or datetime.now(zone).date()
        periods: dict[str, UsagePeriod | None] = {"month": None, "90_days": None}
        if self._summaries:
            periods = aggregate_usage_periods(
                list(self._summaries.items()),
                current_day.replace(day=1),
                current_day - timedelta(days=89),
                current_day,
            )
        environment_periods = aggregate_usage_by_environment(
            list(self._summaries.items()),
            current_day.replace(day=1),
            current_day - timedelta(days=89),
            current_day,
        )
        limits = {
            **{
                key: value
                for environment_limits in self._provider_windows.values()
                for key, value in environment_limits.items()
            },
            **{
                key: value
                for environment_limits in self._source_windows.values()
                for key, value in environment_limits.items()
            },
        }
        self.async_set_updated_data(
            UsageCoordinatorData(
                periods=periods,
                limits=limits,
                summary_errors=len(self._summary_errors),
                summary_updated_at=self._last_summary_update,
                limit_streams_connected=len(self._connected_limit_streams),
                provider_limit_probes_unavailable=sum(
                    self._provider_unavailable.values()
                ),
                usage_limit_source_errors=sum(self._source_errors.values()),
                environment_periods=environment_periods,
                environment_summary_errors={
                    environment_id: environment_id in self._summary_errors
                    for environment_id in self._clients
                },
                environment_summary_updated_at={
                    environment_id: (
                        summary.get("readAt")
                        if isinstance(summary.get("readAt"), str)
                        else None
                    )
                    for environment_id, summary in self._summaries.items()
                },
            )
        )


def _time_zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")
