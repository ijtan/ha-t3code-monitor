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
        status_detail = {
            401: "Authorization failed.",
            403: "The server denied the requested access.",
            404: "The requested endpoint was not found.",
        }.get(err.status)
        if status_detail is None and err.status >= 500:
            status_detail = "The server encountered an error."
        detail = f"The server returned HTTP {err.status}."
        if status_detail is not None:
            detail = f"{detail} {status_detail}"
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
