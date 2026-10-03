"""Ensure setup diagnostics are actionable without disclosing request secrets."""

from aiohttp import ClientResponseError

from custom_components.t3code.errors import request_failure


def test_http_failure_reports_status_without_echoing_response_details() -> None:
    error = ClientResponseError(
        None,
        (),
        status=404,
        message="unexpected content at https://host.invalid/?token=secret",
    )

    diagnostic = str(request_failure("Environment descriptor request", error))

    assert "HTTP 404" in diagnostic
    assert "secret" not in diagnostic
    assert "host.invalid" not in diagnostic


def test_timeout_failure_is_explicit() -> None:
    diagnostic = str(request_failure("Shell snapshot request", TimeoutError()))

    assert diagnostic == "Shell snapshot request: The request timed out."


def test_authorization_failure_is_distinguished_from_other_http_errors() -> None:
    error = ClientResponseError(None, (), status=401)

    diagnostic = str(request_failure("Shell snapshot request", error))

    assert "HTTP 401" in diagnostic
    assert "Authorization failed" in diagnostic
