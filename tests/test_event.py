"""Pushed shell transitions surface on the activity event entity."""

import asyncio
from types import SimpleNamespace

import pytest

import custom_components.t3code as integration
from custom_components.t3code.coordinator import T3CodeCoordinator


def test_pushed_transitions_write_event_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = SimpleNamespace(entry_id="entry-1")
    coordinator = T3CodeCoordinator(
        SimpleNamespace(), {"env-a": ("Alpha", object())}, "Workstation", entry  # type: ignore[arg-type]
    )
    monkeypatch.setattr(
        integration, "T3CodeCoordinator", T3CodeCoordinator, raising=False
    )
    from custom_components.t3code.event import T3CodeEventEntity

    entity = T3CodeEventEntity(coordinator, entry)  # type: ignore[arg-type]
    triggered: list[str] = []
    written: list[list[str]] = []
    monkeypatch.setattr(
        entity,
        "_trigger_event",
        lambda event_type, _data=None: triggered.append(event_type),
    )
    monkeypatch.setattr(
        entity, "async_write_ha_state", lambda: written.append(list(triggered))
    )
    asyncio.run(entity.async_added_to_hass())

    coordinator._handle_items(
        "env-a",
        coordinator._environments["env-a"],
        [
            {
                "kind": "thread-upserted",
                "sequence": 1,
                "thread": {
                    "id": "thread-1",
                    "session": {"status": "running"},
                    "hasPendingApprovals": True,
                },
            }
        ],
    )

    assert written == [
        ["session_created"],
        ["session_created", "approval_required"],
    ]
