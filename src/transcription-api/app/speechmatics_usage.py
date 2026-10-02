from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx


@dataclass(frozen=True)
class SpeechmaticsAPIKey:
    name: str
    value: str = field(repr=False)


@dataclass(frozen=True)
class SpeechmaticsUsage:
    used_hours: float
    limit_hours: float
    percent_used: float | None
    job_count: int
    since: str
    until: str
    reported_hours: float = 0.0
    local_today_hours: float = 0.0


@dataclass(frozen=True)
class SpeechmaticsKeyUsage:
    key: SpeechmaticsAPIKey
    usage: SpeechmaticsUsage | None
    error: str | None = None

    @property
    def available(self) -> bool:
        return self.usage is not None and not self.error


def fetch_speechmatics_usage(
    *,
    api_key: str,
    batch_url: str,
    limit_hours: float = 0.0,
    timeout_seconds: float = 10.0,
    since: str = "",
) -> SpeechmaticsUsage:
    today = datetime.now(timezone.utc).date()
    start = since or today.replace(day=1).isoformat()
    # The provider reports completed UTC days, not live usage.
    end = (today - timedelta(days=1)).isoformat()
    if datetime.fromisoformat(start).date() > today:
        raise ValueError("SPEECHMATICS_USAGE_SINCE cannot be in the future")
    if start[:10] > end:
        return SpeechmaticsUsage(
            0, limit_hours, 0 if limit_hours > 0 else None, 0, start, end
        )
    response = httpx.get(
        f"{batch_url.rstrip('/')}/usage",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=timeout_seconds,
        params={"since": start, "until": end},
    )
    response.raise_for_status()
    return parse_speechmatics_usage(response.json(), limit_hours=limit_hours)


def fetch_speechmatics_key_usages(
    *,
    api_keys: tuple[SpeechmaticsAPIKey, ...],
    batch_url: str,
    limit_hours: float = 0.0,
    timeout_seconds: float = 10.0,
    since: str = "",
) -> list[SpeechmaticsKeyUsage]:
    # Duplicate secrets must not appear as independent budgets.
    unique = tuple({key.value: key for key in reversed(api_keys)}.values())

    def fetch(key: SpeechmaticsAPIKey) -> SpeechmaticsKeyUsage:
        try:
            usage = fetch_speechmatics_usage(
                api_key=key.value,
                batch_url=batch_url,
                limit_hours=limit_hours,
                timeout_seconds=timeout_seconds,
                since=since,
            )
            return SpeechmaticsKeyUsage(key=key, usage=usage)
        except Exception as exc:
            # Never publish provider response bodies or Authorization values.
            if isinstance(exc, httpx.HTTPStatusError):
                message = f"HTTP {exc.response.status_code}"
            else:
                message = type(exc).__name__
            return SpeechmaticsKeyUsage(key=key, usage=None, error=message)

    if not unique:
        return []
    with ThreadPoolExecutor(max_workers=min(4, len(unique))) as pool:
        return list(pool.map(fetch, unique))


def select_speechmatics_api_key(
    *,
    api_keys: tuple[SpeechmaticsAPIKey, ...],
    batch_url: str,
    limit_hours: float = 0.0,
    timeout_seconds: float = 10.0,
    since: str = "",
) -> SpeechmaticsKeyUsage:
    rows = fetch_speechmatics_key_usages(
        api_keys=api_keys,
        batch_url=batch_url,
        limit_hours=limit_hours,
        timeout_seconds=timeout_seconds,
        since=since,
    )
    available = [row for row in rows if row.available and row.usage is not None]
    if not available:
        errors = "; ".join(f"{row.key.name}: {row.error}" for row in rows if row.error)
        raise RuntimeError(
            f"no Speechmatics API key usage available: {errors or 'no keys configured'}"
        )
    return min(available, key=speechmatics_key_usage_score)


def parse_speechmatics_usage(
    payload: dict[str, Any], *, limit_hours: float = 0.0
) -> SpeechmaticsUsage:
    if not isinstance(payload, dict) or "summary" not in payload:
        raise ValueError("Speechmatics usage response is missing summary")
    summary = payload["summary"]
    if summary is None:
        summary = payload.get("details") or []
    if not isinstance(summary, list):
        raise ValueError("Speechmatics usage summary must be an array or null")

    transcription_rows = [
        row
        for row in summary
        if isinstance(row, dict)
        and str(row.get("type", "")).strip().lower() == "transcription"
        and str(row.get("mode", "batch")).strip().lower() == "batch"
    ]
    rows = transcription_rows

    used_hours = sum(_float(row.get("duration_hrs")) for row in rows)
    normalized_limit_hours = max(0.0, limit_hours)
    percent_used = (
        (used_hours / normalized_limit_hours * 100.0)
        if normalized_limit_hours > 0
        else None
    )
    job_count = sum(int(_float(row.get("count"))) for row in rows)

    return SpeechmaticsUsage(
        used_hours=used_hours,
        limit_hours=normalized_limit_hours,
        percent_used=percent_used,
        job_count=job_count,
        since=str(payload.get("since", "") or ""),
        until=str(payload.get("until", "") or ""),
        reported_hours=used_hours,
    )


def format_speechmatics_usage(usage: SpeechmaticsUsage) -> str:
    parts = ["speechmatics usage"]
    if usage.percent_used is not None and usage.limit_hours > 0:
        parts.append(_format_percent(usage.percent_used))
        parts.append(
            f"{_format_hours(usage.used_hours)}/{_format_hours(usage.limit_hours)}"
        )
    else:
        parts.append(f"current={_format_hours(usage.used_hours)}")
    parts.append(f"jobs={usage.job_count}")
    if usage.since:
        parts.append(f"since={usage.since}")
    if usage.until:
        parts.append(f"until={usage.until}")
    return " ".join(parts)


def format_speechmatics_key_usage(row: SpeechmaticsKeyUsage) -> str:
    if row.error or row.usage is None:
        return f"{row.key.name}: unavailable ({row.error or 'unknown error'})"
    usage = row.usage
    if usage.percent_used is not None and usage.limit_hours > 0:
        return (
            f"{row.key.name}: {_format_percent(usage.percent_used)} "
            f"{_format_hours(usage.used_hours)}/{_format_hours(usage.limit_hours)} "
            f"jobs={usage.job_count}"
        )
    return f"{row.key.name}: {_format_hours(usage.used_hours)} jobs={usage.job_count}"


def speechmatics_key_usage_score(row: SpeechmaticsKeyUsage) -> tuple[float, float, str]:
    if row.usage is None:
        return (float("inf"), float("inf"), row.key.name)
    percent = (
        row.usage.percent_used
        if row.usage.percent_used is not None
        else row.usage.used_hours
    )
    return (percent, row.usage.used_hours, row.key.name)


def _float(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("Invalid boolean usage value")
    try:
        number = float(value)
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid numeric usage value") from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError("Usage values must be finite and non-negative")
    return number


def _format_hours(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".") + "h"


def _format_percent(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".") + "%"
