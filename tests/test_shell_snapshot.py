"""Shell requests declare the orchestration protocol T3 expects."""

from __future__ import annotations

import asyncio
from typing import Any, Self
from urllib.parse import parse_qs, urlsplit

from custom_components.t3code.t3_client import T3Client


class _FakeResponse:
    status = 200

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    async def json(self) -> dict[str, Any]:
        return {"snapshotSequence": 1, "threads": []}


class _FakeSession:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}

    def get(self, _url: str, *, headers: dict[str, str]) -> _FakeResponse:
        self.headers = headers
        return _FakeResponse()


def test_shell_snapshot_sends_protocol_header() -> None:
    session = _FakeSession()
    client = T3Client(session, "http://127.0.0.1:3773", "read-token")  # type: ignore[arg-type]

    asyncio.run(client.shell_snapshot())

    assert session.headers["x-t3-orchestration-protocol"] == "2"
    assert session.headers["Authorization"] == "Bearer read-token"


def test_websocket_url_requests_protocol_2() -> None:
    client = T3Client(_FakeSession(), "https://t3.example", "read-token")  # type: ignore[arg-type]

    async def ticket() -> str:
        return "ticket"

    client._websocket_ticket = ticket  # type: ignore[method-assign]
    url = urlsplit(asyncio.run(client._websocket_url()))

    assert url.scheme == "wss"
    assert parse_qs(url.query) == {
        "wsTicket": ["ticket"],
        "orchestrationProtocol": ["2"],
    }
