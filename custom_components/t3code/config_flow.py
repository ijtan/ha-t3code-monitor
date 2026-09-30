"""Config flow for T3 Code environments."""

from __future__ import annotations

import logging
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
from .t3_connect import T3Connect

_LOGGER = logging.getLogger(__name__)


def _safe_origin(base_url: str) -> str:
    """Return only the scheme, hostname, and port for logs and UI diagnostics."""
    try:
        parsed = urlsplit(base_url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return "configured URL"
    if not hostname:
        return "configured URL"
    host = f"[{hostname}]" if ":" in hostname else hostname
    return f"{parsed.scheme}://{host}:{port}" if port else f"{parsed.scheme}://{host}"


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
        return self.async_show_menu(
            step_id="user", menu_options=["direct", "connect"]
        )

    async def async_step_direct(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        diagnostic = ""
        if user_input is not None and CONF_BASE_URL in user_input:
            base_url = user_input[CONF_BASE_URL].strip().rstrip("/")
            try:
                parsed = urlsplit(base_url)
                valid_url = parsed.scheme in {"http", "https"} and bool(parsed.netloc)
            except ValueError:
                valid_url = False
            if not valid_url:
                errors["base"] = "invalid_url"
                diagnostic = "Enter the environment's HTTP or HTTPS base URL, not a T3 web-app or pairing-page URL."
            else:
                try:
                    session = async_get_clientsession(self.hass)
                    environment_id = await T3Client(
                        session, base_url, ""
                    ).environment_id()
                except T3ClientError as err:
                    errors["base"] = "cannot_connect"
                    diagnostic = str(err)
                    _LOGGER.warning(
                        "Setup could not read the T3 environment descriptor at %s: %s",
                        _safe_origin(base_url),
                        diagnostic,
                    )
                else:
                    await self.async_set_unique_id(environment_id)
                    self._abort_if_unique_id_configured()
                    try:
                        access_token = await T3Client.exchange_pairing_credential(
                            session, base_url, user_input[CONF_TOKEN]
                        )
                        client = T3Client(session, base_url, access_token)
                        await client.shell_snapshot()
                    except T3ClientError as err:
                        errors["base"] = "invalid_pairing_credential"
                        diagnostic = str(err)
                        _LOGGER.warning(
                            "Pairing failed for T3 environment at %s: %s",
                            _safe_origin(base_url),
                            diagnostic,
                        )
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
                vol.Required(
                    CONF_NAME,
                    default=user_input.get(CONF_NAME, "T3 Code Monitor")
                    if user_input
                    else "T3 Code Monitor",
                ): str,
                vol.Required(CONF_BASE_URL): str,
                vol.Required(CONF_TOKEN): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
            }
        )
        return self.async_show_form(
            step_id="direct",
            data_schema=schema,
            errors=errors,
            description_placeholders={"diagnostic": diagnostic},
        )

    async def async_step_connect(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Begin T3 Connect OAuth device authorization."""
        if hasattr(self, "_oauth_task"):
            if not self._oauth_task.done():
                return self.async_show_progress(
                    step_id="connect",
                    progress_action="wait_for_authorization",
                    progress_task=self._oauth_task,
                )
            try:
                self._cloud_tokens = self._oauth_task.result()
                records = await self._connect.list_environments(
                    self._cloud_tokens["access_token"]
                )
            except T3ClientError as err:
                _LOGGER.warning("T3 Connect authorization failed: %s", err)
                return self.async_abort(reason="cannot_connect")
            if not records:
                return self.async_abort(reason="no_environments")
            self._environment_records = records
            choices = {
                item["environmentId"]: f"{item['label']} ({item['environmentId']})"
                for item in records
                if isinstance(item, dict)
                and item.get("environmentId")
                and item.get("label")
            }
            self._environment_choices = choices
            return self.async_show_progress_done(next_step_id="connect_environment")
        if not hasattr(self, "_device"):
            try:
                self._connect_name = "T3 Code Monitor"
                self._connect = T3Connect(async_get_clientsession(self.hass))
                self._device = await self._connect.begin_device_authorization()
            except T3ClientError as err:
                _LOGGER.warning("Could not start T3 Connect authorization: %s", err)
                return self.async_abort(reason="cannot_connect")
        if user_input is not None:
            self._oauth_task = self.hass.async_create_task(
                self._connect.poll_device_authorization(self._device)
            )
            return self.async_show_progress(
                step_id="connect",
                progress_action="wait_for_authorization",
                progress_task=self._oauth_task,
            )
        device = self._device
        return self.async_show_form(
            step_id="connect",
            data_schema=vol.Schema({}),
            description_placeholders={
                "verification_uri": device.get("verification_uri_complete")
                or device["verification_uri"],
                "user_code": device["user_code"],
            },
        )

    async def async_step_connect_environment(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Mint an environment credential and exchange it for read-only access."""
        if user_input is None:
            return self.async_show_form(
                step_id="connect_environment",
                data_schema=vol.Schema(
                    {vol.Required("environment_id"): vol.In(self._environment_choices)}
                ),
            )
        environment_id = user_input["environment_id"]
        record = next(
            item
            for item in self._environment_records
            if item["environmentId"] == environment_id
        )
        try:
            endpoint, token = await self._connect.connect_environment(
                self._cloud_tokens["access_token"], environment_id
            )
            client = T3Client(
                async_get_clientsession(self.hass), endpoint, token, self._connect
            )
            await client.shell_snapshot()
        except T3ClientError as err:
            _LOGGER.warning(
                "Could not connect to selected T3 Connect environment: %s", err
            )
            return self.async_abort(reason="cannot_connect")
        await self.async_set_unique_id(environment_id)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=record["label"],
            data={
                CONF_BASE_URL: endpoint,
                CONF_TOKEN: token,
                CONF_NAME: self._connect_name,
                "environment_id": environment_id,
                "connection_type": "connect",
                "dpop_key": self._connect.private_key_pem,
                "cloud_refresh_token": self._cloud_tokens.get("refresh_token", ""),
            },
        )


class T3CodeOptionsFlow(OptionsFlow):
    """Renew the environment session using a newly created pairing credential."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        diagnostic = ""
        if user_input is not None:
            base_url = self.config_entry.data[CONF_BASE_URL]
            session = async_get_clientsession(self.hass)
            try:
                if self.config_entry.data.get("connection_type") == "connect":
                    connect = T3Connect(session, self.config_entry.data["dpop_key"])
                    saved_refresh = self.config_entry.options.get(
                        "cloud_refresh_token",
                        self.config_entry.data.get("cloud_refresh_token", ""),
                    )
                    refreshed = await connect.refresh_cloud_session(saved_refresh)
                    endpoint, access_token = await connect.connect_environment(
                        refreshed["access_token"],
                        self.config_entry.data["environment_id"],
                    )
                    if endpoint != base_url:
                        raise T3ClientError(
                            "T3 Connect returned a changed environment endpoint; reconfigure the integration"
                        )
                    await T3Client(session, endpoint, access_token, connect).shell_snapshot()
                    options = {
                        CONF_TOKEN: access_token,
                        "cloud_refresh_token": refreshed.get("refresh_token", saved_refresh),
                    }
                else:
                    access_token = await T3Client.exchange_pairing_credential(
                        session, base_url, user_input[CONF_TOKEN]
                    )
                    await T3Client(session, base_url, access_token).shell_snapshot()
                    options = {CONF_TOKEN: access_token}
            except T3ClientError as err:
                errors["base"] = "invalid_pairing_credential"
                diagnostic = str(err)
                _LOGGER.warning(
                    "Credential renewal failed for T3 environment at %s: %s",
                    _safe_origin(base_url),
                    diagnostic,
                )
            else:
                return self.async_create_entry(title="", data=options)

        schema = (
            vol.Schema({})
            if self.config_entry.data.get("connection_type") == "connect"
            else vol.Schema(
                {
                    vol.Required(CONF_TOKEN): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.PASSWORD
                        )
                    ),
                }
            )
        )
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
            description_placeholders={"diagnostic": diagnostic},
        )
