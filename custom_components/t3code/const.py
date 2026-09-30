"""Constants for the T3 Code integration."""

from __future__ import annotations

DOMAIN = "t3code"
CONF_BASE_URL = "base_url"
CONF_TOKEN = "token"

PLATFORMS = ["sensor", "binary_sensor"]

SESSION_STATUSES = (
    "starting",
    "running",
    "ready",
    "idle",
    "interrupted",
    "stopped",
    "error",
)

RPC_SUBSCRIBE_SHELL = "orchestration.subscribeShell"
