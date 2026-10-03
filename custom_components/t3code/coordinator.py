"""Coordinate shell streams and aggregate counts across T3 environments."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN
from .errors import T3ClientError
from .metrics import combine_shell_metrics, shell_metrics, thread_events
from .t3_client import T3Client
from .usage_coordinator import T3UsageCoordinator

_LOGGER = logging.getLogger(__name__)
RECONNECT_DELAY = 5
SNAPSHOT_POLL_INTERVAL = 30


@dataclass
class _EnvironmentState:
    """Live stream state for one configured T3 environment."""

    name: str
    client: T3Client
    threads: dict[str, dict[str, Any]] = field(default_factory=dict)
    sequence: int = 0
    stream_connected: bool = False
    snapshot_available: bool = False
    stream_failure_logged: bool = False
    snapshot_failure_logged: bool = False


class T3CodeCoordinator(DataUpdateCoordinator[dict[str, int]]):
    """Maintain environment streams and expose summed shell counters."""

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
            name=f"{DOMAIN}_{name}",
            update_interval=None,
        )
        if not clients:
            raise ValueError("At least one T3 environment client is required")
        self.environment_name = name
        self.entry = entry
        self.entry_id = entry.entry_id
        self._environments = {
            environment_id: _EnvironmentState(name=environment_name, client=client)
            for environment_id, (environment_name, client) in clients.items()
        }
        self.usage = T3UsageCoordinator(hass, clients, name, entry)
        self._runners: list[asyncio.Task[None]] = []
        self._event_listeners: dict[str, list[Callable[[dict[str, Any]], None]]] = {
            "session_created": [],
            "approval_required": [],
            "user_input_required": [],
        }

    def add_event_listener(
        self, event_type: str, listener: Callable[[dict[str, Any]], None]
    ) -> Callable[[], None]:
        """Register a listener for a specific pushed shell event type."""
        if event_type not in self._event_listeners:
            raise ValueError(f"Unsupported T3 Code event type: {event_type}")
        listeners = self._event_listeners[event_type]
        listeners.append(listener)

        def remove_listener() -> None:
            listeners.remove(listener)

        return remove_listener

    @property
    def all_connected(self) -> bool:
        """Whether every environment is reachable by stream or snapshot polling."""
        return all(
            state.stream_connected or state.snapshot_available
            for state in self._environments.values()
        )

    @property
    def environment_ids(self) -> tuple[str, ...]:
        """Configured T3 environment IDs, in their stable setup order."""
        return tuple(self._environments)

    @property
    def environment_count(self) -> int:
        """Number of T3 environments represented by this config entry."""
        return len(self._environments)

    def environment_name(self, environment_id: str) -> str:
        """Return the display name associated with one configured environment."""
        state = self._environments.get(environment_id)
        return state.name if state is not None else environment_id

    def environment_metrics(self, environment_id: str) -> dict[str, int]:
        """Return shell counters for one environment, without aggregation."""
        state = self._environments.get(environment_id)
        return shell_metrics(state.threads) if state is not None else shell_metrics({})

    def environment_connected(self, environment_id: str) -> bool:
        """Whether one environment has a working stream or recent snapshot."""
        state = self._environments.get(environment_id)
        return bool(state and (state.stream_connected or state.snapshot_available))

    def environment_connection_attributes(self, environment_id: str) -> dict[str, Any]:
        """Expose the stream and snapshot health components for one environment."""
        state = self._environments.get(environment_id)
        return {
            "environment_id": environment_id,
            "environment_name": self.environment_name(environment_id),
            "stream_connected": bool(state and state.stream_connected),
            "snapshot_available": bool(state and state.snapshot_available),
        }

    async def async_initialize(self) -> None:
        """Read initial snapshots before forwarding platforms to Home Assistant."""
        errors: list[tuple[str, _EnvironmentState, T3ClientError]] = []
        for environment_id, state in self._environments.items():
            try:
                snapshot = await state.client.shell_snapshot()
                self._apply_snapshot(state, snapshot)
            except asyncio.CancelledError:
                raise
            except T3ClientError as err:
                errors.append((environment_id, state, err))
                _LOGGER.warning(
                    "Initial T3 Code connection failed for environment %s (%s): %s",
                    state.name,
                    environment_id,
                    err,
                )
                continue
            except Exception as err:  # noqa: BLE001 - protect setup from client bugs
                safe_error = T3ClientError(
                    f"Unexpected error ({type(err).__name__}) while reading the shell snapshot."
                )
                errors.append((environment_id, state, safe_error))
                _LOGGER.error(
                    "Unexpected error during initial T3 Code connection for environment %s (%s), error type %s",
                    state.name,
                    environment_id,
                    type(err).__name__,
                )
                continue
            state.snapshot_available = True

        if errors and not any(
            state.snapshot_available for state in self._environments.values()
        ):
            _LOGGER.error(
                "T3 Code setup failed: none of %d configured environments could be reached",
                len(self._environments),
            )
            raise errors[0][2]
        if errors:
            _LOGGER.warning(
                "T3 Code setup reached %d of %d configured environments; "
                "unavailable environments will be retried",
                self.environment_count - len(errors),
                self.environment_count,
            )
        self._publish()

    def start(self) -> None:
        """Start push subscriptions and defensive snapshot polling."""
        self.usage.start()
        for environment_id, state in self._environments.items():
            self._runners.extend(
                (
                    self.entry.async_create_background_task(
                        self.hass,
                        self._run_environment(environment_id, state),
                        f"t3code_{self.entry_id}_{environment_id}_stream",
                    ),
                    self.entry.async_create_background_task(
                        self.hass,
                        self._poll_environment(environment_id, state),
                        f"t3code_{self.entry_id}_{environment_id}_snapshot",
                    ),
                )
            )

    async def stop(self) -> None:
        """Cancel and await all active environment streams."""
        await self.usage.stop()
        for runner in self._runners:
            runner.cancel()
        await asyncio.gather(*self._runners, return_exceptions=True)
        self._runners.clear()

    async def _run_environment(
        self, environment_id: str, state: _EnvironmentState
    ) -> None:
        while True:
            try:
                snapshot = await state.client.shell_snapshot()
                self._apply_snapshot(state, snapshot)
                state.snapshot_available = True
                self._publish()
                async for items in state.client.subscribe_shell(state.sequence):
                    if not state.stream_connected:
                        state.stream_connected = True
                        if state.stream_failure_logged:
                            _LOGGER.info(
                                "T3 Code shell stream reconnected for environment %s (%s)",
                                state.name,
                                environment_id,
                            )
                            state.stream_failure_logged = False
                        self._publish()
                    self._handle_items(environment_id, state, items)
            except asyncio.CancelledError:
                raise
            except T3ClientError as err:
                if not state.stream_failure_logged:
                    _LOGGER.warning(
                        "T3 Code shell stream unavailable for environment %s (%s): %s",
                        state.name,
                        environment_id,
                        err,
                    )
                    state.stream_failure_logged = True
                else:
                    _LOGGER.debug(
                        "T3 Code shell stream retry failed for environment %s (%s): %s",
                        state.name,
                        environment_id,
                        err,
                    )
            except Exception as err:  # noqa: BLE001 - keep reconnect loop alive
                log = _LOGGER.debug if state.stream_failure_logged else _LOGGER.error
                log(
                    "Unexpected error reading T3 Code environment %s (%s), error type %s",
                    state.name,
                    environment_id,
                    type(err).__name__,
                )
                state.stream_failure_logged = True

            state.stream_connected = False
            self._publish()
            await asyncio.sleep(RECONNECT_DELAY)

    async def _poll_environment(
        self, environment_id: str, state: _EnvironmentState
    ) -> None:
        """Refresh the projection periodically in case stream events are missed."""
        while True:
            await asyncio.sleep(SNAPSHOT_POLL_INTERVAL)
            try:
                snapshot = await state.client.shell_snapshot()
                self._apply_snapshot(state, snapshot)
                state.snapshot_available = True
                if state.snapshot_failure_logged:
                    _LOGGER.info(
                        "T3 Code snapshot polling recovered for environment %s (%s)",
                        state.name,
                        environment_id,
                    )
                    state.snapshot_failure_logged = False
            except asyncio.CancelledError:
                raise
            except T3ClientError as err:
                state.snapshot_available = False
                if not state.snapshot_failure_logged:
                    _LOGGER.warning(
                        "T3 Code snapshot polling failed for environment %s (%s): %s",
                        state.name,
                        environment_id,
                        err,
                    )
                    state.snapshot_failure_logged = True
                else:
                    _LOGGER.debug(
                        "T3 Code snapshot retry failed for environment %s (%s): %s",
                        state.name,
                        environment_id,
                        err,
                    )
            except Exception as err:  # noqa: BLE001 - keep polling loop alive
                state.snapshot_available = False
                log = _LOGGER.debug if state.snapshot_failure_logged else _LOGGER.error
                log(
                    "Unexpected error refreshing T3 Code environment %s (%s), "
                    "error type %s",
                    state.name,
                    environment_id,
                    type(err).__name__,
                )
                state.snapshot_failure_logged = True
            self._publish()

    @staticmethod
    def _apply_snapshot(state: _EnvironmentState, snapshot: dict[str, Any]) -> None:
        threads = snapshot.get("threads")
        if not isinstance(threads, list):
            raise T3ClientError("T3 Code returned an invalid shell snapshot")
        sequence = snapshot.get("snapshotSequence", state.sequence)
        if not isinstance(sequence, int):
            raise T3ClientError("T3 Code returned an invalid snapshot sequence")
        if sequence < state.sequence:
            return
        state.threads = {
            thread["id"]: thread
            for thread in threads
            if isinstance(thread, dict) and isinstance(thread.get("id"), str)
        }
        state.sequence = sequence

    def _handle_items(
        self,
        environment_id: str,
        state: _EnvironmentState,
        items: list[dict[str, Any]],
    ) -> None:
        """Apply a pushed chunk and notify on per-thread state transitions."""
        changed = False
        for item in items:
            incoming_thread = item.get("thread")
            thread_id = (
                incoming_thread.get("id") if isinstance(incoming_thread, dict) else None
            )
            previous_thread = (
                state.threads.get(thread_id) if isinstance(thread_id, str) else None
            )
            item_changed = self._apply_stream_item(state, item)
            changed = item_changed or changed
            if (
                not item_changed
                or item.get("kind") != "thread-upserted"
                or not isinstance(thread_id, str)
            ):
                continue
            updated_thread = state.threads[thread_id]
            event_data = {
                "environment_id": environment_id,
                "environment_name": state.name,
                "thread_id": thread_id,
                "thread_title": updated_thread.get("title"),
            }
            for event_type in thread_events(previous_thread, updated_thread):
                for listener in tuple(self._event_listeners[event_type]):
                    listener(event_data)
        if not changed:
            return
        self._publish()

    def _apply_stream_item(
        self, state: _EnvironmentState, item: dict[str, Any]
    ) -> bool:
        kind = item.get("kind")
        if kind == "snapshot":
            snapshot = item.get("snapshot")
            if isinstance(snapshot, dict):
                self._apply_snapshot(state, snapshot)
                return True
            return False
        if kind == "synchronized":
            return False

        sequence = item.get("sequence")
        if isinstance(sequence, int):
            if sequence <= state.sequence:
                return False
            state.sequence = sequence
        if kind == "thread-removed":
            state.threads.pop(item.get("threadId"), None)
        elif kind == "thread-upserted":
            thread = item.get("thread")
            if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
                return False
            state.threads[thread["id"]] = thread
        else:
            return False
        return True

    def _publish(self) -> None:
        counts = [shell_metrics(state.threads) for state in self._environments.values()]
        self.async_set_updated_data(combine_shell_metrics(counts))
