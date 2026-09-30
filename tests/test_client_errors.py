"""Ensure setup diagnostics are actionable without disclosing request secrets."""

from aiohttp import ClientResponseError

from custom_components.t3code.t3_client import _request_failure


def test_http_failure_reports_status_without_echoing_response_details() -> None:
    error = ClientResponseError(
        None,
        (),
        status=404,
        message="unexpected content at https://host.invalid/?token=secret",
    )

    diagnostic = str(_request_failure("Environment descriptor request", error))

    assert "HTTP 404" in diagnostic
    assert "secret" not in diagnostic
    assert "host.invalid" not in diagnostic


def test_timeout_failure_is_explicit() -> None:
    diagnostic = str(_request_failure("Shell snapshot request", TimeoutError()))

    assert diagnostic == "Shell snapshot request: The request timed out."
