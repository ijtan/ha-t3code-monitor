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
