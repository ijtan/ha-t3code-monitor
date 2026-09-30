"""Config flow for T3 Code environments."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_BASE_URL, CONF_TOKEN, DOMAIN
from .t3_client import T3Client, T3ClientError


class T3CodeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up one T3 Code environment."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Allow a new short-lived pairing credential to renew the session."""
        return T3CodeOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            base_url = user_input[CONF_BASE_URL].strip().rstrip("/")
            parsed = urlsplit(base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                errors["base"] = "invalid_url"
            else:
                try:
                    session = async_get_clientsession(self.hass)
                    environment_id = await T3Client(
                        session, base_url, ""
                    ).environment_id()
                except T3ClientError:
                    errors["base"] = "cannot_connect"
                else:
                    await self.async_set_unique_id(environment_id)
                    self._abort_if_unique_id_configured()
                    try:
                        access_token = await T3Client.exchange_pairing_credential(
                            session, base_url, user_input[CONF_TOKEN]
                        )
                        client = T3Client(session, base_url, access_token)
                        await client.shell_snapshot()
                    except T3ClientError:
                        errors["base"] = "invalid_pairing_credential"
                    else:
                        return self.async_create_entry(
                            title=user_input[CONF_NAME].strip(),
                            data={
                                CONF_BASE_URL: base_url,
                                CONF_TOKEN: access_token,
                                CONF_NAME: user_input[CONF_NAME].strip(),
                            },
                        )

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default="T3 Code Monitor"): str,
                vol.Required(CONF_BASE_URL): str,
                vol.Required(CONF_TOKEN): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)


class T3CodeOptionsFlow(OptionsFlow):
    """Renew the environment session using a newly created pairing credential."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            base_url = self.config_entry.data[CONF_BASE_URL]
            session = async_get_clientsession(self.hass)
            try:
                access_token = await T3Client.exchange_pairing_credential(
                    session, base_url, user_input[CONF_TOKEN]
                )
                await T3Client(session, base_url, access_token).shell_snapshot()
            except T3ClientError:
                errors["base"] = "invalid_pairing_credential"
            else:
                return self.async_create_entry(title="", data={CONF_TOKEN: access_token})

        schema = vol.Schema(
            {
                vol.Required(CONF_TOKEN): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
