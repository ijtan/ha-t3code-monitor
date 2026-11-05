"""Load the coordinators the way the platforms use them."""

from types import SimpleNamespace

import pytest

from custom_components.t3code.coordinator import T3CodeCoordinator


@pytest.fixture
def coordinator() -> T3CodeCoordinator:
    clients = {
        "env-a": ("Alpha", object()),
        "env-b": ("Beta", object()),
    }
    entry = SimpleNamespace(entry_id="entry-1")
    return T3CodeCoordinator(SimpleNamespace(), clients, "Workstations", entry)  # type: ignore[arg-type]


def test_environment_names_resolve_per_environment(
    coordinator: T3CodeCoordinator,
) -> None:
    assert coordinator.environment_name("env-a") == "Alpha"
    assert coordinator.environment_name("env-b") == "Beta"
    assert coordinator.environment_name("unknown") == "unknown"
    assert coordinator.entry_name == "Workstations"


def test_usage_environment_names_resolve_per_environment(
    coordinator: T3CodeCoordinator,
) -> None:
    usage = coordinator.usage

    assert usage.environment_name("env-a") == "Alpha"
    assert usage.environment_name("env-b") == "Beta"
    assert usage.entry_name == "Workstations"


def test_offline_environment_reports_its_name(
    coordinator: T3CodeCoordinator,
) -> None:
    assert not coordinator.environment_connected("env-a")
    assert coordinator.environment_connection_attributes("env-a") == {
        "environment_id": "env-a",
        "environment_name": "Alpha",
        "stream_connected": False,
        "snapshot_available": False,
    }


def _thread(thread_id: str, status: str = "running") -> dict:
    return {"id": thread_id, "status": status, "latestRunId": "run-1"}


def test_stream_items_follow_protocol_2_kinds(coordinator: T3CodeCoordinator) -> None:
    state = coordinator._environments["env-a"]
    coordinator._apply_snapshot(
        state,
        {"snapshotSequence": 10, "threads": [_thread("t1"), _thread("t2", "idle")]},
    )

    coordinator._handle_items(
        "env-a",
        state,
        [
            {"kind": "synchronized"},
            {
                "kind": "snapshot",
                "snapshot": {"snapshotSequence": 10, "threads": [], "projects": []},
                "resolvedRepositoryIdentityRoots": ["/src/project"],
            },
            {"kind": "project.updated", "sequence": 11, "project": {"id": "p1"}},
            {
                "kind": "thread.updated",
                "sequence": 12,
                "location": "active",
                "thread": _thread("t3", "completed"),
            },
            {
                "kind": "thread.updated",
                "sequence": 13,
                "location": "archive",
                "thread": _thread("t1"),
            },
            {
                "kind": "thread.removed",
                "sequence": 14,
                "location": "active",
                "threadId": "t2",
            },
            {
                "kind": "thread.updated",
                "sequence": 9,
                "location": "active",
                "thread": _thread("stale"),
            },
        ],
    )

    assert set(state.threads) == {"t3"}
    assert state.sequence == 14
    assert coordinator.data["session_completed"] == 1
    assert coordinator.data["threads"] == 1


def test_archived_thread_update_emits_no_event(
    coordinator: T3CodeCoordinator,
) -> None:
    events: list[dict] = []
    coordinator.add_event_listener("approval_required", events.append)
    state = coordinator._environments["env-a"]
    archived = {
        **_thread("t1"),
        "pendingRuntimeRequest": {"id": "request-1", "kind": "command"},
    }

    coordinator._handle_items(
        "env-a",
        state,
        [
            {
                "kind": "thread.updated",
                "sequence": 1,
                "location": "archive",
                "thread": archived,
            }
        ],
    )

    assert events == []


def _pushed(sequence: int, thread: dict, location: str = "active") -> dict:
    return {
        "kind": "thread.updated",
        "sequence": sequence,
        "location": location,
        "thread": thread,
    }


def test_enrichment_snapshot_keeps_threads(coordinator: T3CodeCoordinator) -> None:
    events: list[dict] = []
    coordinator.add_event_listener("session_created", events.append)
    state = coordinator._environments["env-a"]
    coordinator._apply_snapshot(state, {"snapshotSequence": 1, "threads": [_thread("t1")]})

    coordinator._handle_items(
        "env-a",
        state,
        [
            {
                "kind": "snapshot",
                "snapshot": {"snapshotSequence": 1, "threads": [], "projects": []},
                "resolvedRepositoryIdentityRoots": ["/src/project"],
            },
            _pushed(2, _thread("t1", "completed")),
        ],
    )

    assert set(state.threads) == {"t1"}
    assert events == []


def test_restored_thread_is_not_reported_as_new(
    coordinator: T3CodeCoordinator,
) -> None:
    events: list[dict] = []
    coordinator.add_event_listener("session_created", events.append)
    state = coordinator._environments["env-a"]
    coordinator._apply_snapshot(state, {"snapshotSequence": 1, "threads": [_thread("t1")]})

    coordinator._handle_items(
        "env-a",
        state,
        [
            {"kind": "thread.removed", "sequence": 2, "location": "active", "threadId": "t1"},
            _pushed(3, _thread("t1", "completed")),
        ],
    )

    assert set(state.threads) == {"t1"}
    assert events == []


def test_subagents_count_only_through_pending_requests(
    coordinator: T3CodeCoordinator,
) -> None:
    events: list[str] = []
    for kind in ("session_created", "approval_required"):
        coordinator.add_event_listener(kind, lambda _data, kind=kind: events.append(kind))
    state = coordinator._environments["env-a"]
    subagent = {
        **_thread("child"),
        "lineage": {"relationshipToParent": "subagent", "parentThreadId": "t1"},
        "pendingRuntimeRequest": {"id": "request-1", "kind": "command"},
    }

    coordinator._handle_items("env-a", state, [_pushed(1, subagent)])

    assert events == ["approval_required"]
    assert coordinator.data["threads"] == 0
    assert coordinator.data["session_running"] == 0
    assert coordinator.data["pending_approvals"] == 1
