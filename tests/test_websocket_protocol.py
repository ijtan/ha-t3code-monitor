"""T3 Effect RPC WebSocket wire-format checks."""

import pytest

from custom_components.t3code.const import RPC_SUBSCRIBE_SHELL
from custom_components.t3code.errors import T3ClientError
from custom_components.t3code.t3_client import (
    _request_id_matches,
    _rpc_exit_value,
    _rpc_request,
    _shell_chunk_items,
    _shell_subscription_request,
)


def test_shell_subscription_uses_effect_rpc_json_envelope() -> None:
    assert _shell_subscription_request("1", 42) == {
        "_tag": "Request",
        "id": "1",
        "tag": RPC_SUBSCRIBE_SHELL,
        "payload": {"afterSequence": 42, "requestCompletionMarker": True},
        "headers": [],
    }


def test_shell_chunk_reads_effect_rpc_values_for_matching_request() -> None:
    item = {"kind": "thread-upserted", "sequence": 43, "thread": {"id": "t1"}}

    assert _shell_chunk_items(
        {"_tag": "Chunk", "requestId": "1", "values": [item]}, "1"
    ) == [item]
    assert _shell_chunk_items(
        {"_tag": "Chunk", "requestId": 1, "values": [item]}, "1"
    ) == [item]
    assert (
        _shell_chunk_items({"_tag": "Chunk", "requestId": "2", "values": [item]}, "1")
        is None
    )


def test_shell_chunk_rejects_malformed_values() -> None:
    with pytest.raises(T3ClientError, match="invalid shell WebSocket event"):
        _shell_chunk_items(
            {"_tag": "Chunk", "requestId": "1", "values": ["not-an-event"]},
            "1",
        )


def test_rpc_request_envelope_and_response_ids_match_har_and_effect_json() -> None:
    assert _rpc_request("7", "server.getUsageSummary", {"sinceDay": "2026-07-01"}) == {
        "_tag": "Request",
        "id": "7",
        "tag": "server.getUsageSummary",
        "payload": {"sinceDay": "2026-07-01"},
        "headers": [],
    }
    assert _request_id_matches("7", "7")
    assert _request_id_matches(7, "7")
    assert _rpc_exit_value(
        {
            "_tag": "Exit",
            "requestId": 7,
            "exit": {"_tag": "Success", "value": {"buckets": []}},
        },
        "7",
    ) == {"buckets": []}
