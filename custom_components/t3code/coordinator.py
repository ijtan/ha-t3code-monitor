"""Push coordinator for T3 Code environment shell state."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN
from .metrics import shell_metrics
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
