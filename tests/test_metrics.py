"""Tests for the T3 shell aggregate projection."""

from custom_components.t3code.metrics import (
    combine_shell_metrics,
    shell_metrics,
    thread_events,
)


def test_shell_metrics_counts_statuses_and_attention_flags() -> None:
    threads = {
        "t1": {
            "session": {"status": "running"},
            "hasPendingApprovals": True,
            "hasPendingUserInput": False,
            "latestTurn": {"state": "running"},
        },
        "t2": {
            "session": {"status": "ready"},
            "hasPendingApprovals": False,
            "hasPendingUserInput": True,
            "latestTurn": {"state": "completed"},
        },
        "t3": {"session": None, "hasPendingApprovals": True},
    }

    assert shell_metrics(threads) == {
        "session_starting": 0,
        "session_running": 1,
        "session_ready": 1,
        "session_idle": 0,
        "session_interrupted": 0,
        "session_stopped": 0,
        "session_error": 0,
        "session_missing": 1,
        "pending_approvals": 2,
        "pending_input": 1,
        "running_turns": 1,
        "threads": 3,
    }


def test_thread_events_detect_session_and_attention_transitions() -> None:
    previous = {
        "session": None,
        "hasPendingApprovals": False,
        "hasPendingUserInput": False,
    }
    updated = {
        "session": {"status": "starting"},
        "hasPendingApprovals": True,
        "hasPendingUserInput": True,
    }

    assert thread_events(previous, updated) == (
        "session_created",
        "approval_required",
        "user_input_required",
    )


def test_thread_events_ignore_unchanged_pending_flags() -> None:
    thread = {
        "session": {"status": "running"},
        "hasPendingApprovals": True,
        "hasPendingUserInput": False,
    }

    assert thread_events(thread, thread) == ()


def test_new_request_emits_even_when_aggregate_may_fall() -> None:
    new_thread = {"session": None, "hasPendingApprovals": True}

    assert thread_events(None, new_thread) == ("approval_required",)


def test_combine_metrics_sums_counts_across_environments() -> None:
    first = shell_metrics({"one": {"session": {"status": "running"}}})
    second = shell_metrics({"two": {"hasPendingApprovals": True}})

    combined = combine_shell_metrics([first, second])

    assert combined["session_running"] == 1
    assert combined["pending_approvals"] == 1
    assert combined["threads"] == 2
