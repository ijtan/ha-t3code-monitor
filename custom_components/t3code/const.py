"""Constants for the T3 Code integration."""

from __future__ import annotations

DOMAIN = "t3code"
CONF_BASE_URL = "base_url"
CONF_TOKEN = "token"
CONF_CONNECTION_TYPE = "connection_type"
CONF_ENVIRONMENTS = "environments"
CONF_ENVIRONMENT_ID = "environment_id"
CONF_DPOP_KEY = "dpop_key"
CONF_CLOUD_REFRESH_TOKEN = "cloud_refresh_token"

CONNECTION_TYPE_CONNECT = "connect"
CONNECTION_TYPE_CONNECT_MULTI = "connect_multi"
CONNECTION_TYPE_CONNECT_PAIRING = "connect_pairing"

PLATFORMS = ["sensor", "binary_sensor", "event"]

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
ORCHESTRATION_PROTOCOL_VERSION = 1
