"""T3 Code Home Assistant integration."""

from __future__ import annotations

from typing import cast

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_BASE_URL,
    CONF_CONNECTION_TYPE,
    CONF_DPOP_KEY,
    CONF_ENVIRONMENT_ID,
    CONF_ENVIRONMENTS,
    CONF_TOKEN,
    CONNECTION_TYPE_CONNECT,
    CONNECTION_TYPE_CONNECT_MULTI,
    CONNECTION_TYPE_CONNECT_PAIRING,
    PLATFORMS,
)
from .coordinator import T3CodeCoordinator
from .errors import T3ClientError
from .models import StoredEnvironment
from .t3_client import T3Client
from .t3_connect import T3Connect

type T3CodeConfigEntry = ConfigEntry[T3CodeCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: T3CodeConfigEntry) -> bool:
    """Set up direct or T3 Connect environment clients."""
    session = async_get_clientsession(hass)
    connection_type = entry.data.get(CONF_CONNECTION_TYPE)
    if connection_type in {
        CONNECTION_TYPE_CONNECT_MULTI,
        CONNECTION_TYPE_CONNECT_PAIRING,
    }:
        environments = cast(
            list[StoredEnvironment],
            entry.options.get(CONF_ENVIRONMENTS, entry.data[CONF_ENVIRONMENTS]),
        )
        dpop_key = (
            T3Connect(session, entry.data[CONF_DPOP_KEY]).dpop_key
            if connection_type == CONNECTION_TYPE_CONNECT_MULTI
            else None
        )
        clients = {
            environment[CONF_ENVIRONMENT_ID]: (
                environment["name"],
                T3Client(
                    session,
                    environment["base_url"],
                    environment[CONF_TOKEN],
                    dpop_key,
                ),
            )
            for environment in environments
        }
    else:
        connection_type = entry.data.get(CONF_CONNECTION_TYPE)
        dpop = (
            T3Connect(session, entry.data[CONF_DPOP_KEY]).dpop_key
            if connection_type == CONNECTION_TYPE_CONNECT
            else None
        )
        client = T3Client(
            session,
            entry.data[CONF_BASE_URL],
            entry.options.get(CONF_TOKEN, entry.data[CONF_TOKEN]),
            dpop,
        )
        environment_id = entry.unique_id or entry.entry_id
        clients = {environment_id: (entry.data.get(CONF_NAME, entry.title), client)}

    coordinator = T3CodeCoordinator(
        hass,
        clients,
        entry.data.get(CONF_NAME, entry.title),
        entry,
    )
    try:
        await coordinator.async_initialize()
    except T3ClientError as err:
        raise ConfigEntryNotReady(
            "Could not connect to the configured T3 Code environment"
        ) from err
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.start()
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: T3CodeConfigEntry) -> bool:
    """Unload an environment entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.stop()
    return unloaded


async def async_reload_entry(hass: HomeAssistant, entry: T3CodeConfigEntry) -> None:
    """Reload after the environment credential is renewed."""
    await hass.config_entries.async_reload(entry.entry_id)
