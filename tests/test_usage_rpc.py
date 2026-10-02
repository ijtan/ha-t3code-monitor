"""Round-trip tests for T3 usage and quota RPC wire handling."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, Self

from aiohttp import WSMsgType

from custom_components.t3code.t3_client import T3Client


class _FakeWebSocket:
    def __init__(self, frames: list[dict[str, Any]]) -> None:
        self.frames = frames
        self.sent: list[dict[str, Any]] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def send_json(self, value: dict[str, Any]) -> None:
        self.sent.append(value)

    def __aiter__(self) -> _FakeWebSocket:
        self._remaining = iter(self.frames)
        return self

    async def __anext__(self) -> SimpleNamespace:
        try:
            frame = next(self._remaining)
        except StopIteration:
            raise StopAsyncIteration from None
        return SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps(frame))


class _FakeSession:
    def __init__(self, websocket: _FakeWebSocket) -> None:
        self.websocket = websocket

    def ws_connect(self, *_args: Any, **_kwargs: Any) -> _FakeWebSocket:
        return self.websocket


def _client_with_frames(
    frames: list[dict[str, Any]],
) -> tuple[T3Client, _FakeWebSocket]:
    websocket = _FakeWebSocket(frames)
    client = T3Client(_FakeSession(websocket), "https://t3.example", "read-token")  # type: ignore[arg-type]

    async def websocket_url() -> str:
        return "wss://t3.example/ws?wsTicket=test&orchestrationProtocol=1"

    client._websocket_url = websocket_url  # type: ignore[method-assign]
    return client, websocket


def test_usage_summary_rpc_sends_request_and_accepts_har_numeric_response_id() -> None:
    client, websocket = _client_with_frames(
        [
            {
                "_tag": "Exit",
                "requestId": 1,
                "exit": {
                    "_tag": "Success",
                    "value": {
                        "contractVersion": 6,
                        "buckets": [],
                        "sources": [],
                        "pricing": {"status": "fresh"},
                    },
                },
            }
        ]
    )
    input_data = {
        "sinceDay": "2026-07-04",
        "untilDay": "2026-10-01",
        "timeZone": "Europe/London",
        "resolution": "day",
    }

    result = asyncio.run(client.get_usage_summary(input_data))

    assert result["contractVersion"] == 6
    assert websocket.sent == [
        {
            "_tag": "Request",
            "id": "1",
            "tag": "server.getUsageSummary",
            "payload": input_data,
            "headers": [],
        }
    ]


def test_server_config_stream_acks_chunks_and_yields_limit_updates() -> None:
    config_event = {
        "version": 1,
        "type": "usageLimitSourcesUpdated",
        "payload": {"sources": []},
    }
    client, websocket = _client_with_frames(
        [{"_tag": "Chunk", "requestId": 1, "values": [config_event]}]
    )

    async def collect() -> list[list[dict[str, Any]]]:
        return [events async for events in client.subscribe_server_config()]

    assert asyncio.run(collect()) == [[config_event]]
    assert websocket.sent == [
        {
            "_tag": "Request",
            "id": "1",
            "tag": "subscribeServerConfig",
            "payload": {"usageLimitSources": True},
            "headers": [],
        },
        {"_tag": "Ack", "requestId": "1"},
    ]
