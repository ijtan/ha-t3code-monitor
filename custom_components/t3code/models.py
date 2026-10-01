"""Typed data persisted in T3 Code Monitor config entries and options."""

from __future__ import annotations

from typing import TypedDict


class StoredEnvironment(TypedDict):
    """T3 environment connection credentials in a multi-environment entry."""

    environment_id: str
    name: str
    base_url: str
    token: str
