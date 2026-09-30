"""Push coordinator for T3 Code environment shell state."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN
from .metrics import combine_shell_metrics, shell_metrics
from .t3_client import T3Client, T3ClientError

_LOGGER = logging.getLogger(__name__)
RECONNECT_DELAY = 5


class T3CodeCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Maintain the environment's current shell projection for HA entities."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: T3Client,
        environment_name: str,
        entry_id: str,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{environment_name}",
            update_interval=None,
        )
        self.client = client
        self.environment_name = environment_name
        self.entry_id = entry_id
        self.projects: dict[str, dict[str, Any]] = {}
        self.threads: dict[str, dict[str, Any]] = {}
        self.sequence = 0
        self._runner: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start the long-lived shell subscription task."""
        self._runner = self.hass.async_create_task(
            self._run(), name=f"t3code_{self.entry_id}_stream"
        )

    async def stop(self) -> None:
        """Stop the stream task cleanly."""
        if self._runner:
            self._runner.cancel()
            await asyncio.gather(self._runner, return_exceptions=True)
            self._runner = None

    async def _run(self) -> None:
        first_connection = True
        while True:
            try:
                if first_connection:
                    snapshot = await self.client.shell_snapshot()
                    self._apply_snapshot(snapshot)
                    self._publish()
                    first_connection = False
                async for item in self.client.subscribe_shell(self.sequence):
                    self._handle_item(item)
            except asyncio.CancelledError:
                raise
            except T3ClientError as err:
                _LOGGER.warning(
                    "T3 Code connection unavailable (%s): %s", self.environment_name, err
                )
            except Exception:
                _LOGGER.exception(
                    "Unexpected error reading T3 Code environment %s", self.environment_name
                )
            self.last_update_success = False
            self.async_update_listeners()
            await asyncio.sleep(RECONNECT_DELAY)
            # Every reconnect starts from an authoritative snapshot. Preserve the
            # snapshot's sequence as the cursor so stale updates are not replayed.
            try:
                snapshot = await self.client.shell_snapshot()
                self._apply_snapshot(snapshot)
                self._publish()
            except (T3ClientError, TypeError, ValueError) as err:
                _LOGGER.debug("T3 Code snapshot retry failed: %s", err)

    def _apply_snapshot(self, snapshot: dict[str, Any]) -> None:
        self.projects = {
            item["id"]: item
            for item in snapshot.get("projects", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        self.threads = {
            item["id"]: item
            for item in snapshot.get("threads", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        self.sequence = int(snapshot.get("snapshotSequence", self.sequence))

    def _handle_item(self, item: dict[str, Any]) -> None:
        kind = item.get("kind")
        if kind == "snapshot":
            self._apply_snapshot(item.get("snapshot", {}))
            self._publish()
            return
        if kind == "synchronized":
            return
        sequence = item.get("sequence")
        if isinstance(sequence, int):
            if sequence <= self.sequence:
                return
            self.sequence = sequence
        if kind == "project-upserted":
            project = item.get("project")
            if isinstance(project, dict) and isinstance(project.get("id"), str):
                self.projects[project["id"]] = project
        elif kind == "project-removed":
            self.projects.pop(item.get("projectId"), None)
        elif kind == "thread-removed":
            self.threads.pop(item.get("threadId"), None)
        elif kind == "thread-upserted":
            thread = item.get("thread")
            if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
                return
            self.threads[thread["id"]] = thread
        else:
            return
        self._publish()

    def _publish(self) -> None:
        self.async_set_updated_data(shell_metrics(self.threads))


class T3CodeMultiCoordinator(DataUpdateCoordinator[dict[str, int]]):
    """Monitor multiple T3 environments and publish summed counters."""

    def __init__(
        self,
        hass: HomeAssistant,
        clients: dict[str, tuple[str, T3Client]],
        environment_name: str,
        entry_id: str,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{environment_name}",
            update_interval=None,
        )
        self.clients = clients
        self.environment_name = environment_name
        self.entry_id = entry_id
        self.states: dict[str, dict[str, Any]] = {
            environment_id: {
                "name": name,
                "threads": {},
                "sequence": 0,
                "connected": False,
            }
            for environment_id, (name, _client) in clients.items()
        }
        self._runners: list[asyncio.Task[None]] = []

    async def async_initialize(self) -> None:
        """Load a baseline from each environment before entities are created."""
        errors: list[T3ClientError] = []
        for environment_id, (_name, client) in self.clients.items():
            try:
                snapshot = await client.shell_snapshot()
            except T3ClientError as err:
                errors.append(err)
                continue
            self._apply_snapshot(environment_id, snapshot)
            self.states[environment_id]["connected"] = True
        if errors and not any(state["connected"] for state in self.states.values()):
            raise errors[0]
        self._publish()

    def start(self) -> None:
        """Start one resilient event stream for every selected environment."""
        self._runners = [
            self.hass.async_create_task(
                self._run_environment(environment_id, client),
                name=f"t3code_{self.entry_id}_{environment_id}_stream",
            )
            for environment_id, (_name, client) in self.clients.items()
        ]

    async def stop(self) -> None:
        """Stop all environment streams."""
        for task in self._runners:
            task.cancel()
        await asyncio.gather(*self._runners, return_exceptions=True)
        self._runners.clear()

    async def _run_environment(self, environment_id: str, client: T3Client) -> None:
        state = self.states[environment_id]
        while True:
            try:
                snapshot = await client.shell_snapshot()
                self._apply_snapshot(environment_id, snapshot)
                state["connected"] = True
                self._publish()
                async for item in client.subscribe_shell(state["sequence"]):
                    self._handle_item(environment_id, item)
            except asyncio.CancelledError:
                raise
            except T3ClientError as err:
                _LOGGER.warning(
                    "T3 Code connection unavailable (%s): %s", state["name"], err
                )
            except Exception:
                _LOGGER.exception(
                    "Unexpected error reading T3 Code environment %s", state["name"]
                )
            state["connected"] = False
            self._publish()
            await asyncio.sleep(RECONNECT_DELAY)

    def _apply_snapshot(self, environment_id: str, snapshot: dict[str, Any]) -> None:
        state = self.states[environment_id]
        state["threads"] = {
            item["id"]: item
            for item in snapshot.get("threads", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        state["sequence"] = int(snapshot.get("snapshotSequence", state["sequence"]))

    def _handle_item(self, environment_id: str, item: dict[str, Any]) -> None:
        state = self.states[environment_id]
        kind = item.get("kind")
        if kind == "snapshot":
            self._apply_snapshot(environment_id, item.get("snapshot", {}))
        elif kind != "synchronized":
            sequence = item.get("sequence")
            if isinstance(sequence, int):
                if sequence <= state["sequence"]:
                    return
                state["sequence"] = sequence
            if kind == "thread-removed":
                state["threads"].pop(item.get("threadId"), None)
            elif kind == "thread-upserted":
                thread = item.get("thread")
                if isinstance(thread, dict) and isinstance(thread.get("id"), str):
                    state["threads"][thread["id"]] = thread
            else:
                return
        self._publish()

    def _publish(self) -> None:
        metrics = [shell_metrics(state["threads"]) for state in self.states.values()]
        totals = combine_shell_metrics(metrics)
        self.async_set_updated_data(totals)
        self.last_update_success = all(
            state["connected"] for state in self.states.values()
        )
        self.async_update_listeners()
