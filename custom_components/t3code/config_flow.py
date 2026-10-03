"""Config flow for T3 Code environments."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any, cast
from urllib.parse import urlsplit

import voluptuous as vol
from aiohttp import ClientSession
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_BASE_URL,
    CONF_CLOUD_REFRESH_TOKEN,
    CONF_CONNECTION_TYPE,
    CONF_DPOP_KEY,
    CONF_ENVIRONMENT_ID,
    CONF_ENVIRONMENTS,
    CONF_TOKEN,
    CONNECTION_TYPE_CONNECT,
    CONNECTION_TYPE_CONNECT_MULTI,
    CONNECTION_TYPE_CONNECT_PAIRING,
    DOMAIN,
)
from .errors import T3ClientError
from .models import StoredEnvironment
from .t3_client import T3Client
from .t3_connect import (
    DeviceAuthorization,
    EnvironmentRecord,
    OAuthTokens,
    T3Connect,
)

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


def _environment_selection_schema(choices: dict[str, str]) -> vol.Schema:
    """Build a multi-select schema for the linked Connect environments."""
    return vol.Schema(
        {
            vol.Required(
                CONF_ENVIRONMENT_ID, default=list(choices)
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        {"value": environment_id, "label": label}
                        for environment_id, label in choices.items()
                    ],
                    multiple=True,
                )
            )
        }
    )


def _pairing_credential_schema() -> vol.Schema:
    """Build a password field for the selected environment's one-time credential."""
    return vol.Schema(
        {
            vol.Required(CONF_TOKEN): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            )
        }
    )


async def _pair_environment(
    session: ClientSession,
    record: EnvironmentRecord,
    pairing_credential: str,
) -> StoredEnvironment:
    """Exchange one environment pairing credential and validate read access."""
    base_url = record["endpoint"]["httpBaseUrl"].rstrip("/")
    try:
        parsed = urlsplit(base_url)
        secure_endpoint = (
            parsed.scheme == "https"
            and parsed.hostname is not None
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        secure_endpoint = False
    if not secure_endpoint:
        raise T3ClientError("T3 Connect returned an invalid environment API URL")
    token = await T3Client.exchange_pairing_credential(
        session, base_url, pairing_credential
    )
    await T3Client(session, base_url, token).shell_snapshot()
    return {
        CONF_ENVIRONMENT_ID: record["environmentId"],
        "name": record["label"],
        "base_url": base_url,
        CONF_TOKEN: token,
    }


class T3CodeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up T3 Code environment monitoring."""

    VERSION = 1

    _connect_client: T3Connect | None = None
    _device_authorization: DeviceAuthorization | None = None
    _oauth_task: asyncio.Task[OAuthTokens] | None = None
    _cloud_tokens: OAuthTokens | None = None
    _environment_records: list[EnvironmentRecord] | None = None
    _environment_choices: dict[str, str] | None = None
    _pairing_task: asyncio.Task[StoredEnvironment] | None = None
    _selected_environments: list[EnvironmentRecord] | None = None
    _connected_environments: list[StoredEnvironment] | None = None
    _pairing_index: int = 0

    def __init__(self) -> None:
        """Initialize per-flow OAuth and environment selection state."""
        super().__init__()
        self._connect_client = None
        self._device_authorization = None
        self._oauth_task = None
        self._cloud_tokens = None
        self._environment_records = None
        self._environment_choices = None
        self._pairing_task = None
        self._selected_environments = None
        self._connected_environments = None
        self._pairing_index = 0

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Allow a new short-lived pairing credential to renew the session."""
        return T3CodeOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(step_id="user", menu_options=["direct", "connect"])

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
        if self._oauth_task is not None:
            if not self._oauth_task.done():
                return self.async_show_progress(
                    step_id="connect",
                    progress_action="wait_for_authorization",
                    progress_task=self._oauth_task,
                )
            try:
                self._cloud_tokens = self._oauth_task.result()
                assert self._connect_client is not None
                records = await self._connect_client.list_environments(
                    self._cloud_tokens["access_token"]
                )
            except T3ClientError as err:
                _LOGGER.warning("T3 Connect authorization failed: %s", err)
                return self.async_abort(reason="cannot_connect")
            if not records:
                return self.async_abort(reason="no_environments")
            self._environment_records = records
            self._environment_choices = {
                item["environmentId"]: f"{item['label']} ({item['environmentId']})"
                for item in records
            }
            return self.async_show_progress_done(next_step_id="connect_environment")
        if self._device_authorization is None:
            try:
                self._connect_client = T3Connect(async_get_clientsession(self.hass))
                self._device_authorization = (
                    await self._connect_client.begin_device_authorization()
                )
            except T3ClientError as err:
                _LOGGER.warning("Could not start T3 Connect authorization: %s", err)
                return self.async_abort(reason="cannot_connect")
        if user_input is not None:
            assert self._connect_client is not None
            assert self._device_authorization is not None
            self._oauth_task = self.hass.async_create_task(
                self._connect_client.poll_device_authorization(
                    self._device_authorization
                )
            )
            return self.async_show_progress(
                step_id="connect",
                progress_action="wait_for_authorization",
                progress_task=self._oauth_task,
            )
        assert self._device_authorization is not None
        device = self._device_authorization
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
        """Select environments, then pair directly with each discovered endpoint."""
        assert self._environment_choices is not None
        if user_input is None:
            return self.async_show_form(
                step_id="connect_environment",
                data_schema=_environment_selection_schema(self._environment_choices),
            )
        selected = user_input.get(CONF_ENVIRONMENT_ID)
        if (
            not isinstance(selected, list)
            or not all(isinstance(environment_id, str) for environment_id in selected)
            or not selected
        ):
            return self.async_show_form(
                step_id="connect_environment",
                data_schema=_environment_selection_schema(self._environment_choices),
                errors={"base": "select_environment"},
            )
        assert self._environment_records is not None
        records = {item["environmentId"]: item for item in self._environment_records}
        if any(environment_id not in records for environment_id in selected):
            return self.async_show_form(
                step_id="connect_environment",
                data_schema=_environment_selection_schema(self._environment_choices),
                errors={"base": "select_environment"},
            )
        environment_ids = list(dict.fromkeys(selected))
        unique = hashlib.sha256(",".join(sorted(environment_ids)).encode()).hexdigest()
        await self.async_set_unique_id(f"t3-connect-{unique}")
        self._abort_if_unique_id_configured()
        self._selected_environments = [
            records[environment_id] for environment_id in environment_ids
        ]
        self._connected_environments = []
        self._pairing_index = 0
        return await self.async_step_connect_pairing()

    async def async_step_connect_pairing(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pair the selected environments one at a time using their URLs."""
        assert self._selected_environments is not None
        assert self._connected_environments is not None
        if self._pairing_task is not None:
            if not self._pairing_task.done():
                return self.async_show_progress(
                    step_id="connect_pairing",
                    progress_action="pair_environment",
                    progress_task=self._pairing_task,
                )
            try:
                self._connected_environments.append(self._pairing_task.result())
            except T3ClientError as err:
                record = self._selected_environments[self._pairing_index]
                _LOGGER.warning(
                    "Could not pair T3 Connect environment %s: %s",
                    record["environmentId"],
                    err,
                )
                self._pairing_task = None
                return self.async_show_form(
                    step_id="connect_pairing",
                    data_schema=_pairing_credential_schema(),
                    errors={"base": "invalid_pairing_credential"},
                    description_placeholders={
                        "environment_name": record["label"],
                        "environment_url": record["endpoint"]["httpBaseUrl"],
                    },
                )
            self._pairing_task = None
            self._pairing_index += 1

        if self._pairing_index == len(self._selected_environments):
            return self.async_create_entry(
                title=f"T3 Code Monitor ({len(self._connected_environments)} environments)",
                data={
                    CONF_NAME: "T3 Code Monitor",
                    CONF_CONNECTION_TYPE: CONNECTION_TYPE_CONNECT_PAIRING,
                    CONF_ENVIRONMENTS: self._connected_environments,
                },
            )

        record = self._selected_environments[self._pairing_index]
        if user_input is None:
            return self.async_show_form(
                step_id="connect_pairing",
                data_schema=_pairing_credential_schema(),
                description_placeholders={
                    "environment_name": record["label"],
                    "environment_url": record["endpoint"]["httpBaseUrl"],
                },
            )
        self._pairing_task = self.hass.async_create_task(
            _pair_environment(
                async_get_clientsession(self.hass), record, user_input[CONF_TOKEN]
            )
        )
        return self.async_show_progress(
            step_id="connect_pairing",
            progress_action="pair_environment",
            progress_task=self._pairing_task,
        )


class T3CodeOptionsFlow(OptionsFlow):
    """Renew either a direct pairing session or a T3 Connect session."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        diagnostic = ""
        if user_input is not None:
            base_url = self.config_entry.data.get(CONF_BASE_URL, "")
            session = async_get_clientsession(self.hass)
            connection_type = self.config_entry.data.get(CONF_CONNECTION_TYPE)
            try:
                if connection_type == CONNECTION_TYPE_CONNECT_PAIRING:
                    options = await self._async_renew_connect_pairing(
                        session, user_input
                    )
                elif connection_type == CONNECTION_TYPE_CONNECT_MULTI:
                    options = await self._async_renew_connect_multi(session)
                elif connection_type == CONNECTION_TYPE_CONNECT:
                    options = await self._async_renew_connect_single(session, base_url)
                else:
                    options = await self._async_renew_direct(
                        session, base_url, user_input[CONF_TOKEN]
                    )
            except T3ClientError as err:
                errors["base"] = (
                    "renewal_failed"
                    if connection_type
                    in {CONNECTION_TYPE_CONNECT, CONNECTION_TYPE_CONNECT_MULTI}
                    else "invalid_pairing_credential"
                )
                diagnostic = str(err)
                connection_type = self.config_entry.data.get(CONF_CONNECTION_TYPE)
                if connection_type == CONNECTION_TYPE_CONNECT_PAIRING:
                    environment_id = user_input.get(CONF_ENVIRONMENT_ID, "unknown")
                    _LOGGER.warning(
                        "T3 Code credential renewal failed for environment %s: %s",
                        environment_id,
                        diagnostic,
                    )
                elif connection_type == CONNECTION_TYPE_CONNECT_MULTI:
                    _LOGGER.warning(
                        "T3 Connect environment credential renewal failed: %s",
                        diagnostic,
                    )
                else:
                    _LOGGER.warning(
                        "T3 Code credential renewal failed for the configured environment: %s",
                        diagnostic,
                    )
            else:
                return self.async_create_entry(title="", data=options)

        schema = (
            self._connect_pairing_schema()
            if self.config_entry.data.get(CONF_CONNECTION_TYPE)
            == CONNECTION_TYPE_CONNECT_PAIRING
            else vol.Schema({})
            if self.config_entry.data.get(CONF_CONNECTION_TYPE)
            in {CONNECTION_TYPE_CONNECT, CONNECTION_TYPE_CONNECT_MULTI}
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

    def _connect_pairing_schema(self) -> vol.Schema:
        """Ask which environment to renew and collect its one-time credential."""
        environments = cast(
            list[StoredEnvironment],
            self.config_entry.options.get(
                CONF_ENVIRONMENTS, self.config_entry.data[CONF_ENVIRONMENTS]
            ),
        )
        return vol.Schema(
            {
                vol.Required(CONF_ENVIRONMENT_ID): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            {
                                "value": environment[CONF_ENVIRONMENT_ID],
                                "label": environment["name"],
                            }
                            for environment in environments
                        ]
                    )
                ),
                vol.Required(CONF_TOKEN): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
            }
        )

    async def _async_renew_connect_pairing(
        self, session: ClientSession, user_input: dict[str, Any]
    ) -> dict[str, list[StoredEnvironment]]:
        """Renew one environment using its newly created pairing credential."""
        environments = cast(
            list[StoredEnvironment],
            self.config_entry.options.get(
                CONF_ENVIRONMENTS, self.config_entry.data[CONF_ENVIRONMENTS]
            ),
        )
        environment_id = user_input.get(CONF_ENVIRONMENT_ID)
        environment = next(
            (
                item
                for item in environments
                if item[CONF_ENVIRONMENT_ID] == environment_id
            ),
            None,
        )
        if environment is None:
            raise T3ClientError("Select a configured T3 Code environment")
        access_token = await T3Client.exchange_pairing_credential(
            session, environment["base_url"], user_input[CONF_TOKEN]
        )
        await T3Client(session, environment["base_url"], access_token).shell_snapshot()
        renewed = [
            {**item, CONF_TOKEN: access_token}
            if item[CONF_ENVIRONMENT_ID] == environment_id
            else item
            for item in environments
        ]
        return {CONF_ENVIRONMENTS: renewed}

    async def _async_renew_connect_multi(
        self, session: ClientSession
    ) -> dict[str, Any]:
        """Renew all selected environment sessions from the saved account grant."""
        data = self.config_entry.data
        environments = cast(
            list[StoredEnvironment],
            self.config_entry.options.get(CONF_ENVIRONMENTS, data[CONF_ENVIRONMENTS]),
        )
        if not environments:
            raise T3ClientError("No T3 Connect environments are configured")

        saved_refresh = self.config_entry.options.get(
            CONF_CLOUD_REFRESH_TOKEN, data[CONF_CLOUD_REFRESH_TOKEN]
        )
        connect = T3Connect(session, self.config_entry.data[CONF_DPOP_KEY])
        refreshed = await connect.refresh_cloud_session(saved_refresh)
        updated_environments: list[StoredEnvironment] = []
        for environment in environments:
            environment_id = environment[CONF_ENVIRONMENT_ID]
            try:
                endpoint, access_token = await connect.connect_environment(
                    refreshed["access_token"], environment_id
                )
                if endpoint != environment["base_url"]:
                    raise T3ClientError(
                        "T3 Connect returned a changed endpoint. Reconfigure this entry."
                    )
                await T3Client(
                    session, endpoint, access_token, connect.dpop_key
                ).shell_snapshot()
            except T3ClientError as err:
                raise T3ClientError(f"Environment {environment_id}: {err}") from err
            updated_environments.append({**environment, CONF_TOKEN: access_token})

        updated_refresh = refreshed.get("refresh_token", saved_refresh)
        return {
            CONF_ENVIRONMENTS: updated_environments,
            CONF_CLOUD_REFRESH_TOKEN: updated_refresh,
        }

    async def _async_renew_connect_single(
        self, session: ClientSession, base_url: str
    ) -> dict[str, Any]:
        """Renew an older single-environment T3 Connect entry."""
        data = self.config_entry.data
        connect = T3Connect(session, data[CONF_DPOP_KEY])
        saved_refresh = self.config_entry.options.get(
            CONF_CLOUD_REFRESH_TOKEN, data[CONF_CLOUD_REFRESH_TOKEN]
        )
        refreshed = await connect.refresh_cloud_session(saved_refresh)
        endpoint, access_token = await connect.connect_environment(
            refreshed["access_token"], data[CONF_ENVIRONMENT_ID]
        )
        if endpoint != base_url:
            raise T3ClientError(
                "T3 Connect returned a changed endpoint. Reconfigure this entry."
            )
        await T3Client(
            session, endpoint, access_token, connect.dpop_key
        ).shell_snapshot()
        return {
            CONF_TOKEN: access_token,
            CONF_CLOUD_REFRESH_TOKEN: refreshed.get("refresh_token", saved_refresh),
        }

    @staticmethod
    async def _async_renew_direct(
        session: ClientSession, base_url: str, pairing_credential: str
    ) -> dict[str, str]:
        """Exchange a newly issued direct-pairing credential."""
        access_token = await T3Client.exchange_pairing_credential(
            session, base_url, pairing_credential
        )
        await T3Client(session, base_url, access_token).shell_snapshot()
        return {CONF_TOKEN: access_token}
