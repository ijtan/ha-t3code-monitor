"""Pure shell-state projection helpers."""

from __future__ import annotations

from collections import Counter
from typing import Any

from .const import SESSION_STATUSES


def shell_metrics(threads: dict[str, dict[str, Any]]) -> dict[str, int]:
    """Calculate aggregate counters from the visible shell threads."""
    sessions = Counter(
        thread.get("session", {}).get("status")
        for thread in threads.values()
        if isinstance(thread.get("session"), dict)
    )
    return {
        **{f"session_{status}": sessions[status] for status in SESSION_STATUSES},
        "session_missing": sum(
            1
            for thread in threads.values()
            if not isinstance(thread.get("session"), dict)
        ),
        "pending_approvals": sum(
            bool(thread.get("hasPendingApprovals")) for thread in threads.values()
        ),
        "pending_input": sum(
            bool(thread.get("hasPendingUserInput")) for thread in threads.values()
        ),
        "running_turns": sum(
            thread.get("latestTurn", {}).get("state") == "running"
            for thread in threads.values()
            if isinstance(thread.get("latestTurn"), dict)
        ),
        "threads": len(threads),
    }


def combine_shell_metrics(
    metrics_by_environment: list[dict[str, int]],
) -> dict[str, int]:
    """Sum the same shell counters across selected environments."""
    if not metrics_by_environment:
        return shell_metrics({})
    return {
        key: sum(metrics.get(key, 0) for metrics in metrics_by_environment)
        for key in metrics_by_environment[0]
    }


def thread_events(
    previous_thread: dict[str, Any] | None,
    thread: dict[str, Any],
) -> tuple[str, ...]:
    """Identify new-session and pending-request transitions for one thread."""
    events: list[str] = []
    previous_session = (
        previous_thread.get("session") if previous_thread is not None else None
    )
    if not isinstance(previous_session, dict) and isinstance(
        thread.get("session"), dict
    ):
        events.append("session_created")

    pending_flags = (
        ("hasPendingApprovals", "approval_required"),
        ("hasPendingUserInput", "user_input_required"),
    )
    for field, event_type in pending_flags:
        was_pending = previous_thread is not None and bool(previous_thread.get(field))
        if bool(thread.get(field)) and not was_pending:
            events.append(event_type)
    return tuple(events)
