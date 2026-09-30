"""Minimal async T3 Code HTTP and Effect RPC WebSocket client."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

from aiohttp import ClientError, ClientSession, WSMsgType

from .const import RPC_SUBSCRIBE_SHELL


class T3ClientError(Exception):
    """T3 environment could not be reached or returned an invalid response."""


class T3Client:
    """Read T3 shell state using its authenticated environment endpoints."""

    def __init__(self, session: ClientSession, base_url: str, token: str) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._token = token
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
                        "T3 Code rejected the pairing credential or read-only scope"
                    )
                response.raise_for_status()
                payload = await response.json()
        except T3ClientError:
            raise
        except (ClientError, asyncio.TimeoutError, ValueError) as err:
            raise T3ClientError("Could not exchange the T3 Code pairing credential") from err
        token = payload.get("access_token") if isinstance(payload, dict) else None
        granted_scope = payload.get("scope", "") if isinstance(payload, dict) else ""
        if not isinstance(token, str) or not token or granted_scope != "orchestration:read":
            raise T3ClientError("T3 Code did not issue the requested read-only session")
        return token

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def environment_id(self) -> str:
        """Read the public environment descriptor to deduplicate alternate routes."""
        try:
            async with self._session.get(f"{self._base_url}/.well-known/t3/environment") as response:
                response.raise_for_status()
                payload = await response.json()
        except (ClientError, asyncio.TimeoutError, ValueError) as err:
            raise T3ClientError("Could not read the T3 Code environment descriptor") from err
        environment_id = payload.get("environmentId") if isinstance(payload, dict) else None
        if not isinstance(environment_id, str) or not environment_id:
            raise T3ClientError("T3 Code returned an invalid environment descriptor")
        return environment_id

    async def shell_snapshot(self) -> dict[str, Any]:
        """Fetch a baseline snapshot over authenticated HTTP."""
        try:
            async with self._session.get(
                f"{self._base_url}/api/orchestration/shell", headers=self.headers
            ) as response:
                if response.status in (401, 403):
                    raise T3ClientError("T3 Code rejected the environment token or read scope")
                response.raise_for_status()
                payload = await response.json()
        except T3ClientError:
            raise
        except (ClientError, asyncio.TimeoutError, ValueError) as err:
            raise T3ClientError("Could not read T3 Code shell snapshot") from err
        if not isinstance(payload, dict) or not isinstance(payload.get("threads"), list):
            raise T3ClientError("T3 Code returned an invalid shell snapshot")
        return payload

    async def _websocket_ticket(self) -> str:
        try:
            async with self._session.post(
                f"{self._base_url}/api/auth/websocket-ticket", headers=self.headers
            ) as response:
                if response.status in (401, 403):
                    raise T3ClientError("T3 Code rejected the environment token")
                response.raise_for_status()
                payload = await response.json()
        except T3ClientError:
            raise
        except (ClientError, asyncio.TimeoutError, ValueError) as err:
            raise T3ClientError("Could not obtain a T3 Code WebSocket ticket") from err
        ticket = payload.get("ticket") if isinstance(payload, dict) else None
        if not isinstance(ticket, str) or not ticket:
            raise T3ClientError("T3 Code returned an invalid WebSocket ticket")
        return ticket

    async def subscribe_shell(self, after_sequence: int) -> AsyncIterator[dict[str, Any]]:
        """Subscribe to shell updates, acknowledging each Effect RPC stream chunk."""
        ticket = await self._websocket_ticket()
        parts = urlsplit(self._ws_base)
        ws_url = urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode({"wsTicket": ticket}), "")
        )
        request_id = 1
        try:
            async with self._session.ws_connect(ws_url, heartbeat=30) as ws:
                await ws.send_json(
                    {
                        "jsonrpc": "2.0",
                        "method": RPC_SUBSCRIBE_SHELL,
                        "params": {"afterSequence": after_sequence},
                        "id": request_id,
                        "headers": [],
                    }
                )
                async for message in ws:
                    if message.type == WSMsgType.TEXT:
                        try:
                            decoded = json.loads(message.data)
                        except json.JSONDecodeError as err:
                            raise T3ClientError("T3 Code sent invalid WebSocket JSON") from err
                        frames = decoded if isinstance(decoded, list) else [decoded]
                        for frame in frames:
                            if not isinstance(frame, dict):
                                continue
                            if frame.get("method") == "@effect/rpc/Ping":
                                await ws.send_json(
                                    {"jsonrpc": "2.0", "method": "@effect/rpc/Pong"}
                                )
                            elif (
                                frame.get("chunk") is True
                                and frame.get("id") == request_id
                            ):
                                values = frame.get("result", [])
                                for item in values if isinstance(values, list) else []:
                                    if isinstance(item, dict):
                                        yield item
                                await ws.send_json(
                                    {
                                        "jsonrpc": "2.0",
                                        "method": "@effect/rpc/Ack",
                                        "params": {"requestId": str(request_id)},
                                    }
                                )
                            elif (
                                frame.get("id") == request_id
                                and "chunk" not in frame
                            ):
                                if frame.get("error"):
                                    raise T3ClientError("T3 Code shell subscription failed")
                                return
                    elif message.type in (WSMsgType.CLOSED, WSMsgType.CLOSE, WSMsgType.CLOSING):
                        break
                    elif message.type == WSMsgType.ERROR:
                        raise T3ClientError("T3 Code WebSocket connection failed")
        except T3ClientError:
            raise
        except (ClientError, asyncio.TimeoutError, OSError) as err:
            raise T3ClientError("Could not connect to the T3 Code shell stream") from err
