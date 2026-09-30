"""Read-only T3 Connect client primitives.

The endpoints and OAuth identifiers below are the public production client
configuration used by T3 Code. OAuth and relay credentials are never logged.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
import uuid
from typing import Any
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from .t3_client import T3ClientError, _request_failure

CLERK_FRONTEND = "https://clerk.t3.codes"
OAUTH_CLIENT_ID = "hzxSgY2cH10sDU2r"
RELAY_URL = "https://relay.t3.codes"
SCOPES = "openid profile email offline_access"
RELAY_SCOPE = "environment:connect"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _jwt(key: ec.EllipticCurvePrivateKey, header: dict, payload: dict) -> str:
    content = f"{_b64(json.dumps(header, separators=(',', ':')).encode())}.{_b64(json.dumps(payload, separators=(',', ':')).encode())}"
    der = key.sign(content.encode(), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    return f"{content}.{_b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


def _jwk(key: ec.EllipticCurvePrivateKey) -> dict[str, str]:
    numbers = key.public_key().public_numbers()
    return {
        "kty": "EC",
        "crv": "P-256",
        "x": _b64(numbers.x.to_bytes(32, "big")),
        "y": _b64(numbers.y.to_bytes(32, "big")),
    }


class T3Connect:
    """T3 Connect device OAuth and relay client."""

    def __init__(
        self, session: ClientSession, private_key_pem: str | None = None
    ) -> None:
        self.session = session
        self.key = (
            serialization.load_pem_private_key(private_key_pem.encode(), password=None)
            if private_key_pem
            else ec.generate_private_key(ec.SECP256R1())
        )
        if not isinstance(self.key, ec.EllipticCurvePrivateKey):
            raise T3ClientError("Invalid T3 Connect proof key")
        self.jwk = _jwk(self.key)
        self.thumbprint = _b64(
            hashlib.sha256(
                json.dumps(self.jwk, sort_keys=True, separators=(",", ":")).encode()
            ).digest()
        )

    @property
    def private_key_pem(self) -> str:
        return self.key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()

    def _proof(self, method: str, url: str, access_token: str | None = None) -> str:
        payload: dict[str, Any] = {
            "htm": method.upper(),
            "htu": url,
            "jti": str(uuid.uuid4()),
            "iat": int(time.time()),
        }
        if access_token:
            payload["ath"] = _b64(hashlib.sha256(access_token.encode()).digest())
        return _jwt(
            self.key, {"typ": "dpop+jwt", "alg": "ES256", "jwk": self.jwk}, payload
        )

    @staticmethod
    async def _check_response(response: Any, action: str) -> None:
        """Raise a safe API diagnostic, retaining only documented error fields."""
        if response.status < 400:
            return
        try:
            payload = await response.json(content_type=None)
        except (ValueError, ClientError):
            payload = {}
        code = payload.get("code") if isinstance(payload, dict) else None
        reason = payload.get("reason") if isinstance(payload, dict) else None
        details = ", ".join(
            f"{label}={value}"
            for label, value in (("code", code), ("reason", reason))
            if isinstance(value, str) and value.replace("_", "").isalnum()
        )
        suffix = f" ({details})" if details else ""
        raise T3ClientError(f"{action}: server returned HTTP {response.status}{suffix}.")

    async def begin_device_authorization(self) -> dict[str, Any]:
        url = f"{CLERK_FRONTEND}/oauth/device_authorization"
        try:
            async with self.session.post(
                url, data={"client_id": OAUTH_CLIENT_ID, "scope": SCOPES}
            ) as response:
                response.raise_for_status()
                data = await response.json()
        except (ClientError, asyncio.TimeoutError, ValueError) as err:
            raise _request_failure("T3 Connect device authorization", err) from err
        required = ("device_code", "user_code", "verification_uri", "expires_in")
        if not all(data.get(key) for key in required):
            raise T3ClientError(
                "T3 Connect returned an invalid device authorization response"
            )
        return data

    async def poll_device_authorization(
        self, authorization: dict[str, Any]
    ) -> dict[str, Any]:
        url = f"{CLERK_FRONTEND}/oauth/token"
        interval = max(authorization.get("interval", 5), 1)
        deadline = time.monotonic() + int(authorization["expires_in"])
        while time.monotonic() < deadline:
            await asyncio.sleep(interval)
            try:
                async with self.session.post(
                    url,
                    data={
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                        "device_code": authorization["device_code"],
                        "client_id": OAUTH_CLIENT_ID,
                    },
                ) as response:
                    payload = await response.json(content_type=None)
                    if response.status < 300:
                        if not payload.get("access_token"):
                            raise T3ClientError(
                                "T3 Connect returned no account access token"
                            )
                        if not payload.get("refresh_token"):
                            raise T3ClientError(
                                "T3 Connect did not grant offline access needed for session renewal"
                            )
                        return payload
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
                    raise T3ClientError(
                        f"T3 Connect authorization failed ({code or response.status})"
                    )
            except (ClientError, asyncio.TimeoutError, ValueError) as err:
                raise _request_failure("T3 Connect authorization polling", err) from err
        raise T3ClientError("T3 Connect device code expired. Start setup again.")

    async def refresh_cloud_session(self, refresh_token: str) -> dict[str, Any]:
        """Renew the Clerk account session using the OAuth refresh token."""
        url = f"{CLERK_FRONTEND}/oauth/token"
        try:
            async with self.session.post(
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
            raise _request_failure("T3 Connect account session renewal", err) from err
        if not isinstance(payload, dict) or not payload.get("access_token"):
            raise T3ClientError("T3 Connect did not renew the account session")
        return payload

    async def list_environments(self, clerk_token: str) -> list[dict[str, Any]]:
        url = f"{RELAY_URL}/v1/environments"
        try:
            async with self.session.get(
                url, headers={"Authorization": f"Bearer {clerk_token}"}
            ) as response:
                response.raise_for_status()
                payload = await response.json()
        except (ClientError, asyncio.TimeoutError, ValueError) as err:
            raise _request_failure("T3 Connect environment listing", err) from err
        records = payload.get("environments") if isinstance(payload, dict) else None
        if not isinstance(records, list):
            raise T3ClientError("T3 Connect returned an invalid environment list")
        return records

    async def connect_environment(
        self, clerk_token: str, environment_id: str
    ) -> tuple[str, str]:
        """Connect through relay and exchange the bound credential for read access."""
        token_url = f"{RELAY_URL}/v1/client/dpop-token"
        proof = self._proof("POST", token_url)
        try:
            async with self.session.post(
                token_url,
                headers={"DPoP": proof},
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                    "subject_token": clerk_token,
                    "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
                    "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
                    "resource": RELAY_URL,
                    "scope": RELAY_SCOPE,
                    "client_id": "t3-web",
                },
            ) as response:
                await self._check_response(response, "T3 Connect relay token exchange")
                relay_token = await response.json()
            connect_url = f"{RELAY_URL}/v1/environments/{environment_id}/connect"
            dpop = self._proof("POST", connect_url, relay_token["access_token"])
            async with self.session.post(
                connect_url,
                headers={
                    "Authorization": f"DPoP {relay_token['access_token']}",
                    "DPoP": dpop,
                },
                json={"clientKeyThumbprint": self.thumbprint},
            ) as response:
                await self._check_response(response, "T3 Connect relay environment connection")
                result = await response.json()
        except (ClientError, asyncio.TimeoutError, ValueError, KeyError) as err:
            raise _request_failure("T3 Connect environment connection", err) from err
        endpoint = result.get("endpoint", {}).get("httpBaseUrl")
        credential = result.get("credential")
        if not isinstance(endpoint, str) or not isinstance(credential, str):
            raise T3ClientError("T3 Connect returned an invalid environment connection")
        endpoint = endpoint.rstrip("/")
        parsed = urlsplit(endpoint)
        if parsed.scheme != "https" or not parsed.hostname:
            raise T3ClientError("T3 Connect returned an insecure environment endpoint")
        token_url = f"{endpoint}/oauth/token"
        proof = self._proof("POST", token_url)
        try:
            async with self.session.post(
                token_url,
                headers={"DPoP": proof},
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                    "subject_token": credential,
                    "subject_token_type": "urn:t3:params:oauth:token-type:environment-bootstrap",
                    "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
                    "scope": "orchestration:read",
                    "client_label": "Home Assistant T3 Code Monitor",
                    "client_device_type": "bot",
                },
            ) as response:
                response.raise_for_status()
                issued = await response.json()
        except (ClientError, asyncio.TimeoutError, ValueError) as err:
            raise _request_failure(
                "T3 Connect read-only credential exchange", err
            ) from err
        access_token = issued.get("access_token") if isinstance(issued, dict) else None
        if (
            not isinstance(access_token, str)
            or not access_token
            or issued.get("scope") != "orchestration:read"
        ):
            raise T3ClientError(
                "T3 Code did not issue the requested orchestration:read session"
            )
        return endpoint, access_token
