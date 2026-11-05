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

# A thread's status is its latest run's status, or idle before its first run.
THREAD_STATUSES = (
    "idle",
    "preparing",
    "queued",
    "starting",
    "running",
    "waiting",
    "completed",
    "interrupted",
    "failed",
    "cancelled",
    "rolled_back",
)
APPROVAL_REQUEST_KINDS = (
    "command",
    "file-read",
    "file-change",
    "mcp-elicitation",
    "permission",
)
USER_INPUT_REQUEST_KIND = "user_input"

RPC_SUBSCRIBE_SHELL = "orchestration.subscribeShell"
RPC_SERVER_GET_USAGE_SUMMARY = "server.getUsageSummary"
RPC_SUBSCRIBE_SERVER_CONFIG = "subscribeServerConfig"
ORCHESTRATION_PROTOCOL_VERSION = 2
ORCHESTRATION_PROTOCOL_HEADER = "x-t3-orchestration-protocol"
