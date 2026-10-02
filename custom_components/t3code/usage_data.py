"""Typed projections for T3 usage summaries and provider quota windows."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass(frozen=True)
class UsagePeriod:
    """Merged usage totals for one requested date range."""

    total_tokens: int
    uncached_input_tokens: int
    cached_input_tokens: int
    cache_creation_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cost_usd: float
    cache_savings_usd: float
    records: int
    unpriced_records: int
    provider_reported_records: int
    model_priced_records: int
    source_scanned_files: int
    source_skipped_files: int
    source_malformed_records: int
    source_statuses: dict[str, int]
    pricing_statuses: tuple[str, ...]
    providers: dict[str, dict[str, int | float]]


@dataclass(frozen=True)
class UsageLimitWindow:
    """A provider-reported account quota window."""

    key: str
    name: str
    environment_id: str
    environment_name: str
    provider: str
    provider_instance_id: str | None
    account_source: str | None
    account_id: str | None
    window_id: str
    window_kind: str
    used_percent: float
    checked_at: str | None
    resets_at: str | None
    window_duration_mins: int | None

    @property
    def remaining_percent(self) -> float:
        """Return the provider-reported portion of the quota not yet used."""
        return max(0.0, min(100.0, 100.0 - self.used_percent))


@dataclass(frozen=True)
class UsageCoordinatorData:
    """Latest summaries, provider limits, and non-sensitive fetch status."""

    periods: dict[str, UsagePeriod | None]
    limits: dict[str, UsageLimitWindow]
    summary_errors: int = 0
    summary_updated_at: str | None = None
    limit_streams_connected: int = 0
    provider_limit_probes_unavailable: int = 0
    usage_limit_source_errors: int = 0


@dataclass
class _PeriodAccumulator:
    """Mutable numeric accumulator used only while constructing a period."""

    fields: dict[str, int | float] = field(
        default_factory=lambda: {
            "uncached_input_tokens": 0,
            "cached_input_tokens": 0,
            "cache_creation_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "cost_usd": 0.0,
            "cache_savings_usd": 0.0,
            "records": 0,
            "unpriced_records": 0,
            "provider_reported_records": 0,
            "model_priced_records": 0,
            "source_scanned_files": 0,
            "source_skipped_files": 0,
            "source_malformed_records": 0,
        }
    )
    source_statuses: dict[str, int] = field(default_factory=dict)
    pricing_statuses: set[str] = field(default_factory=set)
    providers: dict[str, dict[str, int | float]] = field(default_factory=dict)

    def add_bucket(self, bucket: dict[str, Any]) -> None:
        totals = bucket.get("totals")
        if not isinstance(totals, dict):
            return
        values = {
            field_name: _nonnegative_int(totals.get(field_name))
            for field_name in (
                "uncachedInputTokens",
                "cachedInputTokens",
                "cacheCreationTokens",
                "outputTokens",
                "reasoningTokens",
            )
        }
        normalized = {
            "uncached_input_tokens": values["uncachedInputTokens"],
            "cached_input_tokens": values["cachedInputTokens"],
            "cache_creation_tokens": values["cacheCreationTokens"],
            "output_tokens": values["outputTokens"],
            "reasoning_tokens": values["reasoningTokens"],
            "cost_usd": _finite_number(bucket.get("costUsd")),
            "cache_savings_usd": _finite_number(bucket.get("cacheSavingsUsd")),
            "records": _nonnegative_int(bucket.get("records")),
            "unpriced_records": _nonnegative_int(bucket.get("unpricedRecords")),
        }
        source = bucket.get("costSource")
        if source == "providerReported":
            normalized["provider_reported_records"] = normalized["records"]
        elif source == "modelPriced":
            normalized["model_priced_records"] = normalized["records"]
        for key, value in normalized.items():
            self.fields[key] += value

        provider = bucket.get("provider")
        if not isinstance(provider, str):
            return
        provider_totals = self.providers.setdefault(
            provider,
            {
                "total_tokens": 0,
                "cost_usd": 0.0,
                "records": 0,
                "unpriced_records": 0,
            },
        )
        token_total = sum(values[name] for name in values if name != "reasoningTokens")
        provider_totals["total_tokens"] += token_total
        provider_totals["cost_usd"] += normalized["cost_usd"]
        provider_totals["records"] += normalized["records"]
        provider_totals["unpriced_records"] += normalized["unpriced_records"]

    def freeze(self) -> UsagePeriod:
        """Create the immutable sensor-facing period value."""
        total_tokens = sum(
            int(self.fields[key])
            for key in (
                "uncached_input_tokens",
                "cached_input_tokens",
                "cache_creation_tokens",
                "output_tokens",
            )
        )
        return UsagePeriod(
            total_tokens=total_tokens,
            uncached_input_tokens=int(self.fields["uncached_input_tokens"]),
            cached_input_tokens=int(self.fields["cached_input_tokens"]),
            cache_creation_tokens=int(self.fields["cache_creation_tokens"]),
            output_tokens=int(self.fields["output_tokens"]),
            reasoning_tokens=int(self.fields["reasoning_tokens"]),
            cost_usd=float(self.fields["cost_usd"]),
            cache_savings_usd=float(self.fields["cache_savings_usd"]),
            records=int(self.fields["records"]),
            unpriced_records=int(self.fields["unpriced_records"]),
            provider_reported_records=int(self.fields["provider_reported_records"]),
            model_priced_records=int(self.fields["model_priced_records"]),
            source_scanned_files=int(self.fields["source_scanned_files"]),
            source_skipped_files=int(self.fields["source_skipped_files"]),
            source_malformed_records=int(self.fields["source_malformed_records"]),
            source_statuses=dict(self.source_statuses),
            pricing_statuses=tuple(sorted(self.pricing_statuses)),
            providers={key: dict(value) for key, value in self.providers.items()},
        )


def provider_limit_windows(
    environment_id: str, environment_name: str, providers: Any
) -> dict[str, UsageLimitWindow]:
    """Extract provider-reported rolling quota windows from server config."""
    windows: dict[str, UsageLimitWindow] = {}
    if not isinstance(providers, list):
        return windows
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        limits = provider.get("usageLimits")
        provider_id = provider.get("instanceId")
        if not isinstance(limits, dict) or not isinstance(provider_id, str):
            continue
        display_name = (
            provider.get("displayName") or provider.get("driver") or "Provider"
        )
        _add_windows(
            windows,
            environment_id,
            environment_name,
            limits,
            provider=str(display_name),
            provider_instance_id=provider_id,
            account_source=None,
            source_id=None,
            account_id=None,
            name_prefix=f"{environment_name} {display_name}",
        )
    return windows


def source_limit_windows(
    environment_id: str, environment_name: str, sources: Any
) -> dict[str, UsageLimitWindow]:
    """Extract quota windows from configured T3 usage-limit sources."""
    windows: dict[str, UsageLimitWindow] = {}
    if not isinstance(sources, list):
        return windows
    for source in sources:
        if not isinstance(source, dict):
            continue
        source_id = source.get("id")
        source_label = source.get("label")
        accounts = source.get("accounts")
        if not isinstance(source_id, str) or not isinstance(accounts, list):
            continue
        for account in accounts:
            if not isinstance(account, dict):
                continue
            limits = account.get("usageLimits")
            account_id = account.get("id")
            if not isinstance(limits, dict) or not isinstance(account_id, str):
                continue
            driver = account.get("driver") or "Account"
            display_name = account.get("plan") or driver
            _add_windows(
                windows,
                environment_id,
                environment_name,
                limits,
                provider=str(driver),
                provider_instance_id=None,
                account_source=str(source_label or "Usage source"),
                source_id=source_id,
                account_id=account_id,
                name_prefix=f"{environment_name} {source_label or 'Usage source'} {display_name}",
            )
    return windows


def unavailable_provider_limit_probes(providers: Any) -> int:
    """Count configured providers whose limit probes are unsupported or failing."""
    if not isinstance(providers, list):
        return 0
    return sum(
        isinstance(provider, dict)
        and isinstance(provider.get("usageLimits"), dict)
        and isinstance(provider["usageLimits"].get("unavailable"), dict)
        for provider in providers
    )


def usage_limit_source_errors(sources: Any) -> int:
    """Count errored usage-limit sources and accounts with unavailable probes."""
    if not isinstance(sources, list):
        return 0
    errors = 0
    for source in sources:
        if not isinstance(source, dict):
            continue
        errors += isinstance(source.get("error"), str) and bool(source["error"])
        accounts = source.get("accounts")
        if not isinstance(accounts, list):
            continue
        errors += sum(
            isinstance(account, dict)
            and isinstance(account.get("usageLimits"), dict)
            and isinstance(account["usageLimits"].get("unavailable"), dict)
            for account in accounts
        )
    return int(errors)


def _add_windows(
    target: dict[str, UsageLimitWindow],
    environment_id: str,
    environment_name: str,
    limits: dict[str, Any],
    *,
    provider: str,
    provider_instance_id: str | None,
    account_source: str | None,
    source_id: str | None,
    account_id: str | None,
    name_prefix: str,
) -> None:
    unavailable = limits.get("unavailable")
    if isinstance(unavailable, dict) and unavailable.get("reason") == "unsupported":
        return
    raw_windows = limits.get("windows")
    if not isinstance(raw_windows, list):
        return
    checked_at = limits.get("checkedAt")
    checked_at = checked_at if isinstance(checked_at, str) else None
    for window in raw_windows:
        if not isinstance(window, dict):
            continue
        window_id = window.get("id")
        used_percent = window.get("usedPercent")
        if (
            not isinstance(window_id, str)
            or not isinstance(used_percent, int | float)
            or isinstance(used_percent, bool)
            or not math.isfinite(float(used_percent))
        ):
            continue
        used = max(0.0, min(100.0, float(used_percent)))
        window_label = str(window.get("label") or window_id)
        key = json.dumps(
            [environment_id, provider_instance_id, source_id, account_id, window_id],
            separators=(",", ":"),
        )
        raw_duration = window.get("windowDurationMins")
        duration = (
            raw_duration
            if isinstance(raw_duration, int) and not isinstance(raw_duration, bool)
            else None
        )
        resets_at = window.get("resetsAt")
        target[key] = UsageLimitWindow(
            key=key,
            name=f"{name_prefix} {window_label} remaining",
            environment_id=environment_id,
            environment_name=environment_name,
            provider=provider,
            provider_instance_id=provider_instance_id,
            account_source=account_source,
            account_id=account_id,
            window_id=window_id,
            window_kind=str(window.get("kind") or "other"),
            used_percent=used,
            checked_at=checked_at,
            resets_at=resets_at if isinstance(resets_at, str) else None,
            window_duration_mins=duration,
        )


def aggregate_usage_periods(
    summaries: list[tuple[str, dict[str, Any]]],
    month_start: date,
    ninety_day_start: date,
    today: date,
) -> dict[str, UsagePeriod]:
    """Merge summaries, de-duplicating physical transcript sources."""
    owners = _source_owners(summaries)
    supplemental_bucket_ids = _supplemental_bucket_ids(summaries, owners)
    accumulators = {"month": _PeriodAccumulator(), "90_days": _PeriodAccumulator()}
    included_summaries: set[str] = set()

    for environment_id, summary in summaries:
        sources = summary.get("sources")
        if not isinstance(sources, list):
            continue
        included_summaries.add(environment_id)
        owned_sources: list[dict[str, Any]] = []
        for source in sources:
            if not isinstance(source, dict) or source.get("status") == "missing":
                continue
            key = _fingerprint(source.get("fingerprint"))
            owner = owners.get(key) if key is not None else None
            if owner is not None and owner[0] == environment_id:
                owned_sources.append(source)
        buckets = summary.get("buckets")
        if not isinstance(buckets, list):
            continue
        owned_paths = {
            (
                source["fingerprint"]["provider"],
                source["fingerprint"]["resolvedHomePath"],
            )
            for source in owned_sources
            if isinstance(source.get("fingerprint"), dict)
            and isinstance(source["fingerprint"].get("provider"), str)
            and isinstance(source["fingerprint"].get("resolvedHomePath"), str)
        }
        provider_counts: dict[str, int] = {}
        for candidate in sources:
            fingerprint = (
                candidate.get("fingerprint") if isinstance(candidate, dict) else None
            )
            provider = (
                fingerprint.get("provider") if isinstance(fingerprint, dict) else None
            )
            if isinstance(provider, str):
                provider_counts[provider] = provider_counts.get(provider, 0) + 1
        owned_providers_without_path = {
            provider
            for provider, _path in owned_paths
            if provider_counts.get(provider) == 1
        }
        owned_provider_paths = owned_paths
        for bucket in buckets:
            if not isinstance(bucket, dict) or not isinstance(
                bucket.get("provider"), str
            ):
                continue
            provider = bucket["provider"]
            bucket_path = bucket.get("sourcePath")
            if id(bucket) not in supplemental_bucket_ids and (
                provider not in owned_providers_without_path
                if bucket_path is None
                else (provider, bucket_path) not in owned_provider_paths
            ):
                continue
            day = _date_value(bucket.get("day"))
            if day is None:
                continue
            if ninety_day_start <= day <= today:
                accumulators["90_days"].add_bucket(bucket)
            if month_start <= day <= today:
                accumulators["month"].add_bucket(bucket)

    # Coverage and pricing describe the source scan, not individual buckets.
    for environment_id, summary in summaries:
        if environment_id not in included_summaries:
            continue
        sources = summary.get("sources")
        if isinstance(sources, list):
            for source in sources:
                if not isinstance(source, dict):
                    continue
                key = _fingerprint(source.get("fingerprint"))
                status = source.get("status")
                if status == "missing":
                    for accumulator in accumulators.values():
                        accumulator.source_statuses["missing"] = (
                            accumulator.source_statuses.get("missing", 0) + 1
                        )
                    continue
                owner = owners.get(key) if key is not None else None
                if owner is None or owner[0] != environment_id:
                    continue
                for accumulator in accumulators.values():
                    accumulator.fields["source_scanned_files"] += _nonnegative_int(
                        source.get("scannedFiles")
                    )
                    accumulator.fields["source_skipped_files"] += _nonnegative_int(
                        source.get("skippedFiles")
                    )
                    accumulator.fields["source_malformed_records"] += _nonnegative_int(
                        source.get("malformedRecords")
                    )
                if isinstance(status, str):
                    for accumulator in accumulators.values():
                        accumulator.source_statuses[status] = (
                            accumulator.source_statuses.get(status, 0) + 1
                        )
        pricing = summary.get("pricing")
        pricing_status = pricing.get("status") if isinstance(pricing, dict) else None
        if isinstance(pricing_status, str):
            for accumulator in accumulators.values():
                accumulator.pricing_statuses.add(pricing_status)

    return {key: accumulator.freeze() for key, accumulator in accumulators.items()}


def _source_owners(
    summaries: list[tuple[str, dict[str, Any]]],
) -> dict[
    tuple[str, str, str, str],
    tuple[str, dict[str, Any], dict[str, Any]],
]:
    """Claim each fingerprint once, preferring complete and recent scans."""
    ordered = sorted(
        summaries,
        key=lambda item: (-_timestamp(item[1].get("readAt")), item[0]),
    )
    owners: dict[
        tuple[str, str, str, str], tuple[str, dict[str, Any], dict[str, Any]]
    ] = {}
    for preferred_status in ("ok", "partial", "failed"):
        for environment_id, summary in ordered:
            sources = summary.get("sources")
            if not isinstance(sources, list):
                continue
            for source in sources:
                if (
                    not isinstance(source, dict)
                    or source.get("status") != preferred_status
                ):
                    continue
                key = _fingerprint(source.get("fingerprint"))
                if key is not None:
                    owners.setdefault(key, (environment_id, source, summary))
    return owners


def _supplemental_bucket_ids(
    summaries: list[tuple[str, dict[str, Any]]],
    owners: dict[tuple[str, str, str, str], tuple[str, dict[str, Any], dict[str, Any]]],
) -> set[int]:
    """Retain unseen cells from newer partial scans after a complete scan."""
    ordered = sorted(
        summaries,
        key=lambda item: (-_timestamp(item[1].get("readAt")), item[0]),
    )
    seen_by_source: dict[tuple[str, str, str, str], set[tuple[Any, ...]]] = {}
    supplemental_ids: set[int] = set()
    for _environment_id, summary in ordered:
        sources = summary.get("sources")
        if not isinstance(sources, list):
            continue
        for source in sources:
            if not isinstance(source, dict) or source.get("status") != "partial":
                continue
            key = _fingerprint(source.get("fingerprint"))
            owner = owners.get(key) if key is not None else None
            if (
                key is None
                or owner is None
                or owner[1].get("status") != "ok"
                or _timestamp(summary.get("readAt"))
                <= _timestamp(owner[2].get("readAt"))
            ):
                continue
            seen = seen_by_source.get(key)
            if seen is None:
                seen = {
                    _bucket_key(bucket)
                    for bucket in _buckets_for_source(owner[2], owner[1])
                }
                seen_by_source[key] = seen
            for bucket in _buckets_for_source(summary, source):
                key_for_bucket = _bucket_key(bucket)
                if key_for_bucket in seen:
                    continue
                seen.add(key_for_bucket)
                supplemental_ids.add(id(bucket))
    return supplemental_ids


def _buckets_for_source(
    summary: dict[str, Any], source: dict[str, Any]
) -> list[dict[str, Any]]:
    fingerprint = source.get("fingerprint")
    sources = summary.get("sources")
    buckets = summary.get("buckets")
    if not isinstance(fingerprint, dict) or not isinstance(sources, list):
        return []
    if not isinstance(buckets, list):
        return []
    provider = fingerprint.get("provider")
    source_path = fingerprint.get("resolvedHomePath")
    if not isinstance(provider, str) or not isinstance(source_path, str):
        return []
    same_provider_count = sum(
        isinstance(candidate, dict)
        and isinstance(candidate.get("fingerprint"), dict)
        and candidate["fingerprint"].get("provider") == provider
        for candidate in sources
    )
    return [
        bucket
        for bucket in buckets
        if isinstance(bucket, dict)
        and bucket.get("provider") == provider
        and (
            bucket.get("sourcePath") == source_path
            or (bucket.get("sourcePath") is None and same_provider_count == 1)
        )
    ]


def _bucket_key(bucket: dict[str, Any]) -> tuple[Any, ...]:
    return (
        bucket.get("day"),
        bucket.get("hourStart"),
        bucket.get("provider"),
        bucket.get("model"),
    )


def _fingerprint(value: Any) -> tuple[str, str, str, str] | None:
    if not isinstance(value, dict):
        return None
    fields = ("hostId", "provider", "resolvedHomePath", "volumeId")
    values = tuple(value.get(field) for field in fields)
    return values if all(isinstance(part, str) for part in values) else None


def _date_value(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _timestamp(value: Any) -> float:
    if not isinstance(value, str):
        return 0.0
    from datetime import datetime

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _nonnegative_int(value: Any) -> int:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else 0
    )


def _finite_number(value: Any) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return 0.0
    number = float(value)
    return number if math.isfinite(number) else 0.0
