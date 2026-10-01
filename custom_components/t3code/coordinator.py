"""Coordinate shell streams and aggregate counts across T3 environments."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN
from .errors import T3ClientError
from .metrics import combine_shell_metrics, shell_metrics
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
                async for item in state.client.subscribe_shell(state.sequence):
                    state.stream_connected = True
                    self._handle_item(state, item)
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

    def _handle_item(self, state: _EnvironmentState, item: dict[str, Any]) -> None:
        kind = item.get("kind")
        if kind == "snapshot":
            snapshot = item.get("snapshot")
            if isinstance(snapshot, dict):
                self._apply_snapshot(state, snapshot)
                self._publish()
            return
        if kind == "synchronized":
            return

        sequence = item.get("sequence")
        if isinstance(sequence, int):
            if sequence <= state.sequence:
                return
            state.sequence = sequence
        if kind == "thread-removed":
            state.threads.pop(item.get("threadId"), None)
        elif kind == "thread-upserted":
            thread = item.get("thread")
            if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
                return
            state.threads[thread["id"]] = thread
        else:
            return
        self._publish()

    def _publish(self) -> None:
        counts = [shell_metrics(state.threads) for state in self._environments.values()]
        self.async_set_updated_data(combine_shell_metrics(counts))
