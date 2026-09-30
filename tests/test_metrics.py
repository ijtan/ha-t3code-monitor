"""Tests for the T3 shell aggregate projection."""

from custom_components.t3code.metrics import shell_metrics


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
