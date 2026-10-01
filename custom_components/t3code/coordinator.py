"""Coordinate shell streams and aggregate counts across T3 environments."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN
from .errors import T3ClientError
from .metrics import combine_shell_metrics, shell_metrics, thread_events
from .t3_client import T3Client

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


class T3CodeCoordinator(DataUpdateCoordinator[dict[str, int]]):
    """Maintain environment streams and expose summed shell counters."""

    def __init__(
        self,
        hass: HomeAssistant,
        clients: dict[str, tuple[str, T3Client]],
        name: str,
        entry_id: str,
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
        self.entry_id = entry_id
        self._environments = {
            environment_id: _EnvironmentState(name=environment_name, client=client)
            for environment_id, (environment_name, client) in clients.items()
        }
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

    async def async_initialize(self) -> None:
        """Read initial snapshots before forwarding platforms to Home Assistant."""
        errors: list[T3ClientError] = []
        for state in self._environments.values():
            try:
                snapshot = await state.client.shell_snapshot()
            except T3ClientError as err:
                errors.append(err)
                continue
            self._apply_snapshot(state, snapshot)
            state.snapshot_available = True

        if errors and not any(
            state.snapshot_available for state in self._environments.values()
        ):
            raise errors[0]
        self._publish()

    def start(self) -> None:
        """Start push subscriptions and defensive snapshot polling."""
        for environment_id, state in self._environments.items():
            self._runners.extend(
                (
                    self.hass.async_create_task(
                        self._run_environment(environment_id, state),
                        name=f"t3code_{self.entry_id}_{environment_id}_stream",
                    ),
                    self.hass.async_create_task(
                        self._poll_environment(environment_id, state),
                        name=f"t3code_{self.entry_id}_{environment_id}_snapshot",
                    ),
                )
            )

    async def stop(self) -> None:
        """Cancel and await all active environment streams."""
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
                    state.stream_connected = True
                    self._handle_items(environment_id, state, items)
            except asyncio.CancelledError:
                raise
            except T3ClientError as err:
                _LOGGER.warning(
                    "T3 Code connection unavailable (%s): %s", state.name, err
                )
            except Exception:
                _LOGGER.exception(
                    "Unexpected error reading T3 Code environment %s", state.name
                )

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
            except asyncio.CancelledError:
                raise
            except T3ClientError as err:
                state.snapshot_available = False
                _LOGGER.warning(
                    "T3 Code snapshot refresh failed (%s): %s", state.name, err
                )
            except Exception:
                state.snapshot_available = False
                _LOGGER.exception(
                    "Unexpected error refreshing T3 Code environment %s", state.name
                )
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
