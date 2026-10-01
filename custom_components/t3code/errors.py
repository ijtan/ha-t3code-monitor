"""Shared request errors and credential-safe HTTP diagnostics."""

from __future__ import annotations

import asyncio

from aiohttp import (
    ClientConnectorError,
    ClientError,
    ClientResponseError,
    ClientSSLError,
    ContentTypeError,
    InvalidURL,
)


class T3ClientError(Exception):
    """A safe, actionable T3 connection failure."""


def request_failure(action: str, err: Exception) -> T3ClientError:
    """Describe HTTP failures without echoing URLs, response bodies, or secrets."""
    if isinstance(err, asyncio.TimeoutError):
        detail = "The request timed out."
    elif isinstance(err, ContentTypeError):
        detail = "The server did not return JSON. Check the environment URL."
    elif isinstance(err, ClientResponseError):
        detail = f"The server returned HTTP {err.status}."
    elif isinstance(err, ClientSSLError):
        detail = "The HTTPS certificate or TLS handshake failed."
    elif isinstance(err, ClientConnectorError):
        reason = getattr(err.os_error, "strerror", None)
        detail = (
            f"The network connection failed: {reason}."
            if reason
            else "The network connection failed."
        )
    elif isinstance(err, InvalidURL):
        detail = "The URL is invalid."
    elif isinstance(err, ValueError):
        detail = "The server returned invalid JSON."
    elif isinstance(err, ClientError):
        detail = "The HTTP request failed."
    else:
        detail = f"The request failed ({type(err).__name__})."
    return T3ClientError(f"{action}: {detail}")
