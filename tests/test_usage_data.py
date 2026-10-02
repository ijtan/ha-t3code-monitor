"""Tests for usage aggregation, source deduplication, and limit projection."""

from datetime import date

from custom_components.t3code.usage_data import (
    UsageLimitWindow,
    aggregate_usage_periods,
    provider_limit_windows,
    source_limit_windows,
    unavailable_provider_limit_probes,
    usage_limit_source_errors,
)


def _source(status: str = "ok") -> dict:
    return {
        "fingerprint": {
            "hostId": "host-a",
            "provider": "codex",
            "resolvedHomePath": "/home/user/.codex",
            "volumeId": "1:2",
        },
        "status": status,
        "scannedFiles": 4,
        "skippedFiles": 1 if status == "partial" else 0,
        "malformedRecords": 1 if status == "partial" else 0,
        "distinctSessions": 1,
    }


def _bucket(day: str, *, source_path: str = "/home/user/.codex") -> dict:
    return {
        "day": day,
        "provider": "codex",
        "model": "test-model",
        "sourcePath": source_path,
        "totals": {
            "uncachedInputTokens": 10,
            "cachedInputTokens": 2,
            "cacheCreationTokens": 1,
            "outputTokens": 5,
            "reasoningTokens": 3,
        },
        "costUsd": 0.25,
        "cacheSavingsUsd": 0.05,
        "costSource": "modelPriced",
        "records": 1,
        "unpricedRecords": 0,
    }


def _summary(
    *,
    day_buckets: list[dict] | None = None,
    source_status: str = "ok",
    read_at: str = "2026-10-01T00:00:00Z",
) -> dict:
    return {
        "readAt": read_at,
        "sources": [_source(source_status)],
        "buckets": day_buckets or [],
        "pricing": {"status": "fresh"},
    }


def test_aggregate_usage_calculates_month_and_rolling_periods() -> None:
    summaries = [
        (
            "env",
            _summary(
                day_buckets=[
                    _bucket("2026-09-30"),
                    _bucket("2026-10-01"),
                    _bucket("2026-07-03"),
                ]
            ),
        )
    ]

    periods = aggregate_usage_periods(
        summaries,
        month_start=date(2026, 10, 1),
        ninety_day_start=date(2026, 7, 4),
        today=date(2026, 10, 1),
    )

    assert periods["month"].total_tokens == 18
    assert periods["month"].reasoning_tokens == 3
    assert periods["month"].cost_usd == 0.25
    assert periods["90_days"].total_tokens == 36
    assert periods["90_days"].records == 2
    assert periods["90_days"].source_statuses == {"ok": 1}
    assert periods["90_days"].source_scanned_files == 4
    assert periods["90_days"].source_skipped_files == 0
    assert periods["90_days"].pricing_statuses == ("fresh",)
    assert periods["90_days"].providers["codex"]["total_tokens"] == 36


def test_duplicate_physical_usage_source_is_counted_once() -> None:
    older = _summary(
        day_buckets=[_bucket("2026-10-01")],
        read_at="2026-10-01T00:00:00Z",
    )
    newer = _summary(
        day_buckets=[_bucket("2026-10-01")],
        read_at="2026-10-01T01:00:00Z",
    )

    periods = aggregate_usage_periods(
        [("first", older), ("second", newer)],
        date(2026, 10, 1),
        date(2026, 7, 4),
        date(2026, 10, 1),
    )

    assert periods["month"].total_tokens == 18
    assert periods["month"].cost_usd == 0.25
    assert periods["month"].source_statuses == {"ok": 1}
    assert periods["month"].source_skipped_files == 0


def test_complete_source_beats_newer_partial_duplicate() -> None:
    complete = _summary(
        day_buckets=[_bucket("2026-10-01")],
        source_status="ok",
        read_at="2026-10-01T00:00:00Z",
    )
    partial = _summary(
        day_buckets=[_bucket("2026-10-01")],
        source_status="partial",
        read_at="2026-10-01T02:00:00Z",
    )

    periods = aggregate_usage_periods(
        [("complete", complete), ("partial", partial)],
        date(2026, 10, 1),
        date(2026, 7, 4),
        date(2026, 10, 1),
    )

    assert periods["month"].total_tokens == 18
    assert periods["month"].source_statuses == {"ok": 1}


def test_new_cells_from_a_later_partial_scan_are_added_without_duplicate_cells() -> (
    None
):
    complete = _summary(
        day_buckets=[_bucket("2026-10-01")],
        source_status="ok",
        read_at="2026-10-01T00:00:00Z",
    )
    partial_new_bucket = _bucket("2026-10-01") | {"model": "new-model"}
    partial = _summary(
        day_buckets=[_bucket("2026-10-01"), partial_new_bucket],
        source_status="partial",
        read_at="2026-10-01T02:00:00Z",
    )

    periods = aggregate_usage_periods(
        [("complete", complete), ("partial", partial)],
        date(2026, 10, 1),
        date(2026, 7, 4),
        date(2026, 10, 1),
    )

    assert periods["month"].total_tokens == 36
    assert periods["month"].records == 2


def test_valid_empty_summary_has_zero_totals_and_reports_missing_sources() -> None:
    empty = {
        "readAt": "2026-10-01T00:00:00Z",
        "sources": [{**_source("missing"), "status": "missing"}],
        "buckets": [],
        "pricing": {"status": "unavailable"},
    }

    periods = aggregate_usage_periods(
        [("env", empty)],
        date(2026, 10, 1),
        date(2026, 7, 4),
        date(2026, 10, 1),
    )

    assert periods["month"].total_tokens == 0
    assert periods["month"].source_statuses == {"missing": 1}
    assert periods["month"].pricing_statuses == ("unavailable",)


def test_partial_source_exposes_skipped_file_and_malformed_record_coverage() -> None:
    summary = _summary(
        day_buckets=[_bucket("2026-10-01")],
        source_status="partial",
    )

    periods = aggregate_usage_periods(
        [("env", summary)],
        date(2026, 10, 1),
        date(2026, 7, 4),
        date(2026, 10, 1),
    )

    assert periods["month"].source_statuses == {"partial": 1}
    assert periods["month"].source_scanned_files == 4
    assert periods["month"].source_skipped_files == 1
    assert periods["month"].source_malformed_records == 1


def test_limit_remaining_is_clamped_and_does_not_claim_exact_tokens() -> None:
    window = UsageLimitWindow(
        key="key",
        name="Provider weekly remaining",
        environment_id="env",
        environment_name="T3 host",
        provider="Provider",
        provider_instance_id="provider-instance",
        account_source=None,
        account_id=None,
        window_id="weekly",
        window_kind="weekly",
        used_percent=37.5,
        checked_at="2026-10-01T00:00:00Z",
        resets_at="2026-10-08T00:00:00Z",
        window_duration_mins=10080,
    )

    assert window.remaining_percent == 62.5


def test_provider_limit_windows_preserve_quota_window_metadata() -> None:
    windows = provider_limit_windows(
        "env-1",
        "Laptop",
        [
            {
                "instanceId": "codex-main",
                "driver": "codex",
                "displayName": "Codex",
                "usageLimits": {
                    "checkedAt": "2026-10-01T12:00:00Z",
                    "windows": [
                        {
                            "id": "five_hour",
                            "kind": "session",
                            "label": "5 hour",
                            "usedPercent": 37.5,
                            "resetsAt": "2026-10-01T17:00:00Z",
                            "windowDurationMins": 300,
                        }
                    ],
                },
            }
        ],
    )

    assert len(windows) == 1
    window = next(iter(windows.values()))
    assert window.name == "Laptop Codex 5 hour remaining"
    assert window.remaining_percent == 62.5
    assert window.resets_at == "2026-10-01T17:00:00Z"
    assert window.window_duration_mins == 300


def test_usage_limit_sources_keep_accounts_distinct_without_exposing_ids_in_names() -> (
    None
):
    windows = source_limit_windows(
        "env-1",
        "Laptop",
        [
            {
                "id": "proxy-source",
                "label": "CLI Proxy",
                "accounts": [
                    {
                        "id": "account-secret-id",
                        "driver": "claude",
                        "plan": "Claude Pro",
                        "usageLimits": {
                            "checkedAt": "2026-10-01T12:00:00Z",
                            "windows": [
                                {
                                    "id": "weekly",
                                    "kind": "weekly",
                                    "label": "Weekly",
                                    "usedPercent": 140,
                                }
                            ],
                        },
                    },
                    {
                        "id": "second-account",
                        "driver": "claude",
                        "plan": "Claude Pro",
                        "usageLimits": {
                            "checkedAt": "2026-10-01T12:00:00Z",
                            "windows": [
                                {
                                    "id": "weekly",
                                    "kind": "weekly",
                                    "label": "Weekly",
                                    "usedPercent": 25,
                                }
                            ],
                        },
                    },
                ],
            }
        ],
    )

    assert len(windows) == 2
    first, second = windows.values()
    assert first.key != second.key
    assert "account-secret-id" not in first.name
    assert first.remaining_percent == 0
    assert second.remaining_percent == 75


def test_limit_coverage_counts_unsupported_provider_and_source_probes() -> None:
    assert (
        unavailable_provider_limit_probes(
            [
                {"usageLimits": {"unavailable": {"reason": "unsupported"}}},
                {"usageLimits": {"windows": []}},
                {},
            ]
        )
        == 1
    )
    assert (
        usage_limit_source_errors(
            [
                {
                    "error": "source refresh failed",
                    "accounts": [
                        {"usageLimits": {"unavailable": {"reason": "probeFailed"}}}
                    ],
                },
                {"accounts": [{"usageLimits": {"windows": []}}]},
            ]
        )
        == 2
    )


def test_explicitly_unsupported_provider_clears_stale_quota_windows() -> None:
    windows = provider_limit_windows(
        "env-1",
        "Laptop",
        [
            {
                "instanceId": "api-key-provider",
                "driver": "openai-compatible",
                "usageLimits": {
                    "checkedAt": "2026-10-01T12:00:00Z",
                    "windows": [],
                    "unavailable": {"reason": "unsupported"},
                },
            }
        ],
    )

    assert windows == {}
