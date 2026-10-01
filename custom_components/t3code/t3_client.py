"""Minimal async T3 Code HTTP and Effect RPC WebSocket client."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit, urlunsplit

from aiohttp import ClientError, ClientSession, WSMsgType

from .const import ORCHESTRATION_PROTOCOL_VERSION, RPC_SUBSCRIBE_SHELL
from .errors import T3ClientError, request_failure

_LOGGER = logging.getLogger(__name__)


class DpopProofSigner(Protocol):
    """Minimum DPoP interface required by an environment client."""

    def create_proof(
        self, method: str, url: str, access_token: str | None = None
    ) -> str: ...


def _shell_subscription_request(request_id: str, after_sequence: int) -> dict[str, Any]:
    """Build a request using T3's Effect RPC JSON wire format."""
    return {
        "_tag": "Request",
        "id": request_id,
        "tag": RPC_SUBSCRIBE_SHELL,
        "payload": {
            "afterSequence": after_sequence,
            "requestCompletionMarker": True,
        },
        "headers": [],
    }


def _shell_chunk_items(
    frame: dict[str, Any], request_id: str
) -> list[dict[str, Any]] | None:
    """Decode one matching Effect RPC stream chunk, ignoring unrelated frames."""
    if frame.get("_tag") != "Chunk" or frame.get("requestId") != request_id:
        return None
    values = frame.get("values")
    if not isinstance(values, list):
        raise T3ClientError("T3 Code sent an invalid shell WebSocket chunk")
    if not all(isinstance(item, dict) for item in values):
        raise T3ClientError("T3 Code sent an invalid shell WebSocket event")
    return values


class T3Client:
    """Read T3 shell state using its authenticated environment endpoints."""

    def __init__(
        self,
        session: ClientSession,
        base_url: str,
        token: str,
        dpop: DpopProofSigner | None = None,
    ) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._dpop = dpop
        parts = urlsplit(self._base_url)
        ws_scheme = "wss" if parts.scheme == "https" else "ws"
        self._ws_base = urlunsplit((ws_scheme, parts.netloc, "/ws", "", ""))

    @staticmethod
    async def exchange_pairing_credential(
        session: ClientSession, base_url: str, credential: str
    ) -> str:
        """Exchange a one-time pairing credential for a read-only bearer session."""
        form = {
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": credential.strip(),
            "subject_token_type": "urn:t3:params:oauth:token-type:environment-bootstrap",
            "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "scope": "orchestration:read",
            "client_label": "Home Assistant T3 Code",
            "client_device_type": "bot",
        }
        try:
            async with session.post(
                f"{base_url.rstrip('/')}/oauth/token",
                data=form,
                headers={"Accept": "application/json"},
            ) as response:
                if response.status in (400, 401, 403):
                    raise T3ClientError(
                        f"Pairing credential exchange was rejected (HTTP {response.status}). "
                        "It may be expired, already used, or not authorized for orchestration:read."
                    )
                response.raise_for_status()
                payload = await response.json()
        except T3ClientError:
            raise
        except (ClientError, asyncio.TimeoutError, ValueError) as err:
            raise request_failure("Pairing credential exchange", err) from err
        token = payload.get("access_token") if isinstance(payload, dict) else None
        granted_scope = payload.get("scope", "") if isinstance(payload, dict) else ""
        if (
            not isinstance(token, str)
            or not token
            or granted_scope != "orchestration:read"
        ):
            raise T3ClientError("T3 Code did not issue the requested read-only session")
        return token

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"{'DPoP' if self._dpop else 'Bearer'} {self._token}"}

    def _auth_headers(self, method: str, url: str) -> dict[str, str]:
        headers = self.headers
        if self._dpop:
            headers["DPoP"] = self._dpop.create_proof(method, url, self._token)
        return headers

    async def environment_id(self) -> str:
        """Read the public environment descriptor to deduplicate alternate routes."""
        try:
            async with self._session.get(
                f"{self._base_url}/.well-known/t3/environment"
            ) as response:
                response.raise_for_status()
                payload = await response.json()
        except (ClientError, asyncio.TimeoutError, ValueError) as err:
            raise request_failure("Environment descriptor request", err) from err
        environment_id = (
            payload.get("environmentId") if isinstance(payload, dict) else None
        )
        if not isinstance(environment_id, str) or not environment_id:
            raise T3ClientError("T3 Code returned an invalid environment descriptor")
        return environment_id

    async def shell_snapshot(self) -> dict[str, Any]:
        """Fetch a baseline snapshot over authenticated HTTP."""
        try:
            url = f"{self._base_url}/api/orchestration/shell"
            async with self._session.get(
                url, headers=self._auth_headers("GET", url)
            ) as response:
                if response.status in (401, 403):
                    raise T3ClientError(
                        "T3 Code rejected the environment token or read scope"
                    )
                response.raise_for_status()
                payload = await response.json()
        except T3ClientError:
            raise
        except (ClientError, asyncio.TimeoutError, ValueError) as err:
            raise request_failure("Shell snapshot request", err) from err
        if not isinstance(payload, dict) or not isinstance(
            payload.get("threads"), list
        ):
            raise T3ClientError("T3 Code returned an invalid shell snapshot")
        return payload

    async def _websocket_ticket(self) -> str:
        try:
            url = f"{self._base_url}/api/auth/websocket-ticket"
            async with self._session.post(
                url, headers=self._auth_headers("POST", url)
            ) as response:
                if response.status in (401, 403):
                    raise T3ClientError("T3 Code rejected the environment token")
                response.raise_for_status()
                payload = await response.json()
        except T3ClientError:
            raise
        except (ClientError, asyncio.TimeoutError, ValueError) as err:
            raise request_failure("WebSocket ticket request", err) from err
        ticket = payload.get("ticket") if isinstance(payload, dict) else None
        if not isinstance(ticket, str) or not ticket:
            raise T3ClientError("T3 Code returned an invalid WebSocket ticket")
        return ticket

    async def subscribe_shell(
        self, after_sequence: int
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """Subscribe to shell updates, acknowledging each Effect RPC stream chunk."""
        ticket = await self._websocket_ticket()
        parts = urlsplit(self._ws_base)
        ws_url = urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                parts.path,
                urlencode(
                    {
                        "wsTicket": ticket,
                        "orchestrationProtocol": str(ORCHESTRATION_PROTOCOL_VERSION),
                    }
                ),
                "",
            )
        )
        request_id = "1"
        try:
            async with self._session.ws_connect(ws_url, heartbeat=30) as ws:
                _LOGGER.debug("T3 Code shell WebSocket connected")
                await ws.send_json(
                    _shell_subscription_request(request_id, after_sequence)
                )
                async for message in ws:
                    if message.type == WSMsgType.TEXT:
                        try:
                            decoded = json.loads(message.data)
                        except json.JSONDecodeError as err:
                            raise T3ClientError(
                                "T3 Code sent invalid WebSocket JSON"
                            ) from err
                        frames = decoded if isinstance(decoded, list) else [decoded]
                        for frame in frames:
                            if not isinstance(frame, dict):
                                continue
                            message_type = frame.get("_tag")
                            if message_type == "Ping":
                                await ws.send_json({"_tag": "Pong"})
                            elif message_type == "Chunk":
                                shell_items = _shell_chunk_items(frame, request_id)
                                if shell_items is None:
                                    continue
                                if shell_items:
                                    _LOGGER.debug(
                                        "T3 Code shell WebSocket delivered %d update(s)",
                                        len(shell_items),
                                    )
                                yield shell_items
                                await ws.send_json(
                                    {
                                        "_tag": "Ack",
                                        "requestId": request_id,
                                    }
                                )
                            elif (
                                message_type == "Exit"
                                and frame.get("requestId") == request_id
                            ):
                                exit_value = frame.get("exit")
                                if (
                                    isinstance(exit_value, dict)
                                    and exit_value.get("_tag") == "Failure"
                                ):
                                    raise T3ClientError(
                                        "T3 Code shell subscription failed"
                                    )
                                return
                            elif message_type in {"Defect", "ClientProtocolError"}:
                                raise T3ClientError(
                                    "T3 Code shell subscription protocol failed"
                                )
                    elif message.type in (
                        WSMsgType.CLOSED,
                        WSMsgType.CLOSE,
                        WSMsgType.CLOSING,
                    ):
                        break
                    elif message.type == WSMsgType.ERROR:
                        raise T3ClientError("T3 Code WebSocket connection failed")
        except T3ClientError:
            raise
        except (ClientError, asyncio.TimeoutError, OSError) as err:
            raise request_failure("Shell WebSocket connection", err) from err
