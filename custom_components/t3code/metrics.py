"""Pure shell-state projection helpers."""

from __future__ import annotations

from collections import Counter
from typing import Any

from .const import (
    APPROVAL_REQUEST_KINDS,
    THREAD_STATUSES,
    USER_INPUT_REQUEST_KIND,
)


def shell_metrics(threads: dict[str, dict[str, Any]]) -> dict[str, int]:
    """Calculate aggregate counters from the visible shell threads.

    Subagents and side chats count only through their pending requests, as
    T3 lists them inside their parent thread.
    """
    listed = [thread for thread in threads.values() if is_listed_thread(thread)]
    statuses = Counter(thread.get("status") for thread in listed)
    requests = Counter(_pending_request_kind(thread) for thread in threads.values())
    return {
        **{f"session_{status}": statuses[status] for status in THREAD_STATUSES},
        "pending_approvals": sum(requests[kind] for kind in APPROVAL_REQUEST_KINDS),
        "pending_input": requests[USER_INPUT_REQUEST_KIND],
        "running_turns": sum(thread.get("activeRunId") is not None for thread in listed),
        "threads": len(listed),
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
    """Identify first-run and new pending-request transitions for one thread."""
    events: list[str] = []
    previous_run = (
        previous_thread.get("latestRunId") if previous_thread is not None else None
    )
    if (
        previous_run is None
        and thread.get("latestRunId") is not None
        and is_listed_thread(thread)
    ):
        events.append("session_created")

    request = thread.get("pendingRuntimeRequest")
    previous_request = (
        previous_thread.get("pendingRuntimeRequest")
        if previous_thread is not None
        else None
    )
    if isinstance(request, dict) and (
        not isinstance(previous_request, dict)
        or previous_request.get("id") != request.get("id")
    ):
        kind = request.get("kind")
        if kind in APPROVAL_REQUEST_KINDS:
            events.append("approval_required")
        elif kind == USER_INPUT_REQUEST_KIND:
            events.append("user_input_required")
    return tuple(events)


def is_listed_thread(thread: dict[str, Any]) -> bool:
    """Whether T3 shows the thread in its thread list, unlike subagents and side chats."""
    lineage = thread.get("lineage")
    if not isinstance(lineage, dict):
        return True
    return (
        lineage.get("relationshipToParent") != "subagent"
        and lineage.get("sideChat") is not True
    )


def _pending_request_kind(thread: dict[str, Any]) -> str | None:
    request = thread.get("pendingRuntimeRequest")
    return request.get("kind") if isinstance(request, dict) else None
