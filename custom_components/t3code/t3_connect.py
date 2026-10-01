"""T3 Connect OAuth, relay access, and environment credential exchange."""

from __future__ import annotations

import asyncio
import time
from typing import NotRequired, TypedDict
from urllib.parse import quote, urlsplit

from aiohttp import ClientError, ClientResponse, ClientSession

from .dpop import DpopKey
from .errors import T3ClientError, request_failure

CLERK_FRONTEND = "https://clerk.t3.codes"
OAUTH_CLIENT_ID = "hzxSgY2cH10sDU2r"
RELAY_URL = "https://relay.t3.codes"
OAUTH_SCOPES = "openid profile email offline_access"
RELAY_CONNECT_SCOPE = "environment:connect"
ENVIRONMENT_READ_SCOPE = "orchestration:read"
_SAFE_RELAY_CODES = frozenset(
    {
        "auth_invalid",
        "environment_connect_not_authorized",
        "environment_endpoint_unavailable",
        "environment_link_proof_expired",
        "environment_link_proof_invalid",
    }
)
_SAFE_RELAY_REASONS = frozenset(
    {
        "client_proof_key_thumbprint_missing",
        "database_unavailable",
        "endpoint_provider_not_managed",
        "endpoint_request_failed",
        "endpoint_response_invalid",
        "environment_link_not_found",
        "internal_error",
        "invalid_bearer",
        "invalid_dpop",
        "invalid_signature_or_payload",
        "managed_endpoint_allocation_not_found",
        "managed_endpoint_allocation_not_ready",
        "managed_endpoint_base_domain_not_configured",
        "managed_endpoint_hostname_invalid",
        "managed_endpoint_mismatch",
        "missing_bearer",
        "not_authorized",
        "persistence_failed",
        "replayed_nonce",
        "upstream_unavailable",
    }
)


class DeviceAuthorization(TypedDict):
    """Validated response from the OAuth device authorization endpoint."""

    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: NotRequired[int]
    verification_uri_complete: NotRequired[str]


class OAuthTokens(TypedDict):
    """Account tokens returned by OAuth device or refresh grants."""

    access_token: str
    refresh_token: NotRequired[str]


class EnvironmentRecord(TypedDict):
    """A T3 environment already linked to the authenticated account."""

    environmentId: str
    label: str
    endpoint: EnvironmentEndpoint


class EnvironmentEndpoint(TypedDict):
    """Public HTTP and WebSocket routes advertised by T3 Connect."""

    httpBaseUrl: str
    wsBaseUrl: str


def _json_object(value: object, response_name: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise T3ClientError(f"T3 Connect returned an invalid {response_name}")
    return value


def _required_string(payload: dict[str, object], key: str, response_name: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise T3ClientError(f"T3 Connect returned an invalid {response_name}")
    return value


def _optional_string(payload: dict[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise T3ClientError("T3 Connect returned an invalid token response")
    return value


def _oauth_tokens(value: object) -> OAuthTokens:
    payload = _json_object(value, "OAuth token response")
    tokens: OAuthTokens = {
        "access_token": _required_string(
            payload, "access_token", "OAuth token response"
        )
    }
    refresh_token = _optional_string(payload, "refresh_token")
    if refresh_token is not None:
        tokens["refresh_token"] = refresh_token
    return tokens


class T3Connect:
    """Async client for the public T3 Connect device and relay APIs."""

    def __init__(
        self, session: ClientSession, private_key_pem: str | None = None
    ) -> None:
        self._session = session
        self.dpop_key = (
            DpopKey.from_pem(private_key_pem)
            if private_key_pem is not None
            else DpopKey.generate()
        )

    @property
    def private_key_pem(self) -> str:
        """Serialize the proof key for config-entry storage."""
        return self.dpop_key.private_key_pem

    async def begin_device_authorization(self) -> DeviceAuthorization:
        """Request a user code for the OAuth device authorization flow."""
        url = f"{CLERK_FRONTEND}/oauth/device_authorization"
        try:
            async with self._session.post(
                url,
                data={"client_id": OAUTH_CLIENT_ID, "scope": OAUTH_SCOPES},
            ) as response:
                response.raise_for_status()
                payload = _json_object(await response.json(), "device authorization")
        except (ClientError, asyncio.TimeoutError, ValueError) as err:
            raise request_failure("T3 Connect device authorization", err) from err

        expires_in = payload.get("expires_in")
        interval = payload.get("interval")
        if not isinstance(expires_in, int) or expires_in <= 0:
            raise T3ClientError("T3 Connect returned an invalid device authorization")
        if interval is not None and (not isinstance(interval, int) or interval <= 0):
            raise T3ClientError("T3 Connect returned an invalid polling interval")

        authorization: DeviceAuthorization = {
            "device_code": _required_string(
                payload, "device_code", "device authorization"
            ),
            "user_code": _required_string(payload, "user_code", "device authorization"),
            "verification_uri": _required_string(
                payload, "verification_uri", "device authorization"
            ),
            "expires_in": expires_in,
        }
        verification_uri_complete = _optional_string(
            payload, "verification_uri_complete"
        )
        if verification_uri_complete is not None:
            authorization["verification_uri_complete"] = verification_uri_complete
        if interval is not None:
            authorization["interval"] = interval
        return authorization

    async def poll_device_authorization(
        self, authorization: DeviceAuthorization
    ) -> OAuthTokens:
        """Poll until the user approves, denies, or the device code expires."""
        url = f"{CLERK_FRONTEND}/oauth/token"
        interval = authorization.get("interval", 5)
        deadline = time.monotonic() + authorization["expires_in"]
        while time.monotonic() < deadline:
            await asyncio.sleep(interval)
            try:
                async with self._session.post(
                    url,
                    data={
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                        "device_code": authorization["device_code"],
                        "client_id": OAUTH_CLIENT_ID,
                    },
                ) as response:
                    payload = _json_object(
                        await response.json(content_type=None), "OAuth token response"
                    )
                    if response.status < 300:
                        tokens = _oauth_tokens(payload)
                        if "refresh_token" not in tokens:
                            raise T3ClientError(
                                "T3 Connect did not grant offline access needed for renewal"
                            )
                        return tokens
                    code = payload.get("error")
                    if code == "authorization_pending":
                        continue
                    if code == "slow_down":
                        interval += 5
                        continue
                    if code == "access_denied":
                        raise T3ClientError("T3 Connect authorization was denied")
                    if code == "expired_token":
                        break
                    if isinstance(code, str) and code.replace("_", "").isalnum():
                        raise T3ClientError(f"T3 Connect authorization failed ({code})")
                    raise T3ClientError(
                        f"T3 Connect authorization failed (HTTP {response.status})"
                    )
            except (ClientError, asyncio.TimeoutError, ValueError) as err:
                raise request_failure("T3 Connect authorization polling", err) from err
        raise T3ClientError("T3 Connect device code expired. Start setup again.")

    async def refresh_cloud_session(self, refresh_token: str) -> OAuthTokens:
        """Renew the Clerk account session using its OAuth refresh token."""
        url = f"{CLERK_FRONTEND}/oauth/token"
        try:
            async with self._session.post(
                url,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": OAUTH_CLIENT_ID,
                },
            ) as response:
                response.raise_for_status()
                payload = await response.json()
        except (ClientError, asyncio.TimeoutError, ValueError) as err:
            raise request_failure("T3 Connect account session renewal", err) from err
        return _oauth_tokens(payload)

    async def list_environments(self, clerk_token: str) -> list[EnvironmentRecord]:
        """Return environments linked to the signed-in T3 Connect account."""
        url = f"{RELAY_URL}/v1/environments"
        try:
            async with self._session.get(
                url, headers={"Authorization": f"Bearer {clerk_token}"}
            ) as response:
                response.raise_for_status()
                payload = _json_object(await response.json(), "environment list")
        except (ClientError, asyncio.TimeoutError, ValueError) as err:
            raise request_failure("T3 Connect environment listing", err) from err

        environments = payload.get("environments")
        if not isinstance(environments, list):
            raise T3ClientError("T3 Connect returned an invalid environment list")
        records: list[EnvironmentRecord] = []
        for value in environments:
            record = _json_object(value, "environment record")
            endpoint = _json_object(record.get("endpoint"), "environment endpoint")
            records.append(
                {
                    "environmentId": _required_string(
                        record, "environmentId", "environment record"
                    ),
                    "label": _required_string(record, "label", "environment record"),
                    "endpoint": {
                        "httpBaseUrl": _required_string(
                            endpoint, "httpBaseUrl", "environment endpoint"
                        ),
                        "wsBaseUrl": _required_string(
                            endpoint, "wsBaseUrl", "environment endpoint"
                        ),
                    },
                }
            )
        return records

    async def connect_environment(
        self, clerk_token: str, environment_id: str
    ) -> tuple[str, str]:
        """Connect through the relay and exchange its credential for read access."""
        relay_token = await self._exchange_relay_token(clerk_token)
        endpoint, credential = await self._request_environment_credential(
            relay_token, environment_id
        )
        access_token = await self._exchange_environment_credential(endpoint, credential)
        return endpoint, access_token

    async def _exchange_relay_token(self, clerk_token: str) -> str:
        url = f"{RELAY_URL}/v1/client/dpop-token"
        proof = self.dpop_key.create_proof("POST", url)
        try:
            async with self._session.post(
                url,
                headers={"DPoP": proof},
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                    "subject_token": clerk_token,
                    "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
                    "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
                    "resource": RELAY_URL,
                    "scope": RELAY_CONNECT_SCOPE,
                    "client_id": "t3-web",
                },
            ) as response:
                await self._check_response(response, "T3 Connect relay token exchange")
                payload = _json_object(await response.json(), "relay token response")
        except (ClientError, asyncio.TimeoutError, ValueError) as err:
            raise request_failure("T3 Connect relay token exchange", err) from err
        return _required_string(payload, "access_token", "relay token response")

    async def _request_environment_credential(
        self, relay_token: str, environment_id: str
    ) -> tuple[str, str]:
        url = f"{RELAY_URL}/v1/environments/{quote(environment_id, safe='')}/connect"
        proof = self.dpop_key.create_proof("POST", url, relay_token)
        try:
            async with self._session.post(
                url,
                headers={
                    "Authorization": f"DPoP {relay_token}",
                    "DPoP": proof,
                },
                json={"clientKeyThumbprint": self.dpop_key.thumbprint},
            ) as response:
                await self._check_response(
                    response, "T3 Connect relay environment connection"
                )
                payload = _json_object(await response.json(), "environment connection")
        except (ClientError, asyncio.TimeoutError, ValueError) as err:
            raise request_failure(
                "T3 Connect relay environment connection", err
            ) from err

        endpoint_data = _json_object(payload.get("endpoint"), "environment endpoint")
        endpoint = _required_string(
            endpoint_data, "httpBaseUrl", "environment endpoint"
        )
        credential = _required_string(payload, "credential", "environment connection")
        endpoint = endpoint.rstrip("/")
        try:
            parsed = urlsplit(endpoint)
            valid_endpoint = (
                parsed.scheme == "https"
                and parsed.hostname is not None
                and parsed.username is None
                and parsed.password is None
                and not parsed.query
                and not parsed.fragment
            )
        except ValueError:
            valid_endpoint = False
        if not valid_endpoint:
            raise T3ClientError("T3 Connect returned an insecure environment endpoint")
        return endpoint, credential

    async def _exchange_environment_credential(
        self, endpoint: str, credential: str
    ) -> str:
        url = f"{endpoint}/oauth/token"
        proof = self.dpop_key.create_proof("POST", url)
        try:
            async with self._session.post(
                url,
                headers={"DPoP": proof},
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                    "subject_token": credential,
                    "subject_token_type": "urn:t3:params:oauth:token-type:environment-bootstrap",
                    "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
                    "scope": ENVIRONMENT_READ_SCOPE,
                    "client_label": "Home Assistant T3 Code Monitor",
                    "client_device_type": "bot",
                },
            ) as response:
                await self._check_response(
                    response, "T3 Connect read-only credential exchange"
                )
                payload = _json_object(
                    await response.json(), "environment token response"
                )
        except (ClientError, asyncio.TimeoutError, ValueError) as err:
            raise request_failure(
                "T3 Connect read-only credential exchange", err
            ) from err
        access_token = _required_string(
            payload, "access_token", "environment token response"
        )
        scope = _required_string(payload, "scope", "environment token response")
        if scope != ENVIRONMENT_READ_SCOPE:
            raise T3ClientError("T3 Code did not grant orchestration:read")
        return access_token

    @staticmethod
    async def _check_response(response: ClientResponse, action: str) -> None:
        """Include only allowlisted relay error fields in safe diagnostics."""
        if response.status < 400:
            return
        try:
            payload = await response.json(content_type=None)
        except (ValueError, ClientError):
            payload = {}
        code = payload.get("code") if isinstance(payload, dict) else None
        reason = payload.get("reason") if isinstance(payload, dict) else None
        details = [
            f"{label}={value}"
            for label, value in (("code", code), ("reason", reason))
            if isinstance(value, str)
            and value in (_SAFE_RELAY_CODES if label == "code" else _SAFE_RELAY_REASONS)
        ]
        suffix = f" ({', '.join(details)})" if details else ""
        raise T3ClientError(
            f"{action}: server returned HTTP {response.status}{suffix}."
        )
