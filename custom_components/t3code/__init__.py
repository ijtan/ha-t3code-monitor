"""T3 Code Home Assistant integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_BASE_URL, CONF_TOKEN, PLATFORMS
from .coordinator import T3CodeCoordinator
from .metrics import shell_metrics
from .t3_client import T3Client, T3ClientError
from .t3_connect import T3Connect

type T3CodeConfigEntry = ConfigEntry[T3CodeCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: T3CodeConfigEntry) -> bool:
    """Set up a T3 Code environment entry."""
    session = async_get_clientsession(hass)
    dpop = (
        T3Connect(session, entry.data["dpop_key"])
        if entry.data.get("connection_type") == "connect"
        else None
    )
    client = T3Client(
        session,
        entry.data[CONF_BASE_URL],
        entry.options.get(CONF_TOKEN, entry.data[CONF_TOKEN]),
        dpop,
    )
    coordinator = T3CodeCoordinator(
        hass,
        client,
        entry.data.get(CONF_NAME, entry.title),
        entry.entry_id,
    )
    # Validate and establish initial aggregate data before forwarding platforms.
    try:
        snapshot = await client.shell_snapshot()
    except T3ClientError as err:
        raise ConfigEntryNotReady(
            "Could not connect to the configured T3 Code environment"
        ) from err
    coordinator._apply_snapshot(snapshot)
    coordinator.async_set_updated_data(shell_metrics(coordinator.threads))
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.start()
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: T3CodeConfigEntry) -> bool:
    """Unload an environment entry."""
    coordinator = entry.runtime_data
    await coordinator.stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_reload_entry(hass: HomeAssistant, entry: T3CodeConfigEntry) -> None:
    """Reload after the environment credential is renewed."""
    await hass.config_entries.async_reload(entry.entry_id)
