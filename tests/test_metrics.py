"""Tests for the T3 shell aggregate projection."""

from custom_components.t3code.metrics import (
    combine_shell_metrics,
    shell_metrics,
    thread_events,
)


def _request(request_id: str, kind: str) -> dict:
    return {"id": request_id, "kind": kind, "createdAt": "2026-11-05T00:00:00Z"}


def test_shell_metrics_counts_statuses_and_attention_requests() -> None:
    threads = {
        "t1": {
            "status": "running",
            "activeRunId": "run-1",
            "pendingRuntimeRequest": _request("r1", "command"),
        },
        "t2": {
            "status": "waiting",
            "activeRunId": "run-2",
            "pendingRuntimeRequest": _request("r2", "user_input"),
        },
        "t3": {"status": "idle", "activeRunId": None, "pendingRuntimeRequest": None},
        "t4": {
            "status": "rolled_back",
            "activeRunId": None,
            "pendingRuntimeRequest": _request("r4", "auth_refresh"),
        },
    }

    assert shell_metrics(threads) == {
        "session_idle": 1,
        "session_preparing": 0,
        "session_queued": 0,
        "session_starting": 0,
        "session_running": 1,
        "session_waiting": 1,
        "session_completed": 0,
        "session_interrupted": 0,
        "session_failed": 0,
        "session_cancelled": 0,
        "session_rolled_back": 1,
        "pending_approvals": 1,
        "pending_input": 1,
        "running_turns": 2,
        "threads": 4,
    }


def test_thread_events_detect_first_run_and_new_requests() -> None:
    previous = {"latestRunId": None, "pendingRuntimeRequest": None}
    updated = {
        "latestRunId": "run-1",
        "pendingRuntimeRequest": _request("r1", "file-change"),
    }

    assert thread_events(previous, updated) == (
        "session_created",
        "approval_required",
    )
    assert thread_events(
        updated,
        {**updated, "pendingRuntimeRequest": _request("r2", "user_input")},
    ) == ("user_input_required",)


def test_thread_events_ignore_unchanged_requests_and_later_runs() -> None:
    thread = {
        "latestRunId": "run-1",
        "pendingRuntimeRequest": _request("r1", "permission"),
    }

    assert thread_events(thread, thread) == ()
    assert thread_events(thread, {**thread, "latestRunId": "run-2"}) == ()


def test_thread_events_ignore_requests_that_are_not_approvals_or_input() -> None:
    thread = {"latestRunId": "run-1", "pendingRuntimeRequest": None}

    assert (
        thread_events(
            thread,
            {**thread, "pendingRuntimeRequest": _request("r1", "dynamic_tool_call")},
        )
        == ()
    )


def test_new_request_emits_even_when_aggregate_may_fall() -> None:
    new_thread = {
        "latestRunId": None,
        "pendingRuntimeRequest": _request("r1", "command"),
    }

    assert thread_events(None, new_thread) == ("approval_required",)


def test_combine_metrics_sums_counts_across_environments() -> None:
    first = shell_metrics({"one": {"status": "running"}})
    second = shell_metrics(
        {"two": {"status": "idle", "pendingRuntimeRequest": _request("r", "command")}}
    )

    combined = combine_shell_metrics([first, second])

    assert combined["session_running"] == 1
    assert combined["pending_approvals"] == 1
    assert combined["threads"] == 2
