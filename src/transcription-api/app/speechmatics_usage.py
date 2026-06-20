from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class SpeechmaticsAPIKey:
    name: str
    value: str


@dataclass(frozen=True)
class SpeechmaticsUsage:
    used_hours: float
    limit_hours: float
    percent_used: float | None
    job_count: int
    since: str
    until: str


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
    limit_hours: float = 50.0,
    timeout_seconds: float = 10.0,
) -> SpeechmaticsUsage:
    response = httpx.get(
        f"{batch_url.rstrip('/')}/usage",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    return parse_speechmatics_usage(response.json(), limit_hours=limit_hours)


def fetch_speechmatics_key_usages(
    *,
    api_keys: tuple[SpeechmaticsAPIKey, ...],
    batch_url: str,
    limit_hours: float = 50.0,
    timeout_seconds: float = 10.0,
) -> list[SpeechmaticsKeyUsage]:
    rows: list[SpeechmaticsKeyUsage] = []
    for api_key in api_keys:
        try:
            usage = fetch_speechmatics_usage(
                api_key=api_key.value,
                batch_url=batch_url,
                limit_hours=limit_hours,
                timeout_seconds=timeout_seconds,
            )
        except Exception as exc:
            rows.append(SpeechmaticsKeyUsage(key=api_key, usage=None, error=str(exc)))
            continue
        rows.append(SpeechmaticsKeyUsage(key=api_key, usage=usage))
    return rows


def select_speechmatics_api_key(
    *,
    api_keys: tuple[SpeechmaticsAPIKey, ...],
    batch_url: str,
    limit_hours: float = 50.0,
    timeout_seconds: float = 10.0,
) -> SpeechmaticsKeyUsage:
    rows = fetch_speechmatics_key_usages(
        api_keys=api_keys,
        batch_url=batch_url,
        limit_hours=limit_hours,
        timeout_seconds=timeout_seconds,
    )
    available = [row for row in rows if row.available and row.usage is not None]
    if not available:
        errors = "; ".join(f"{row.key.name}: {row.error}" for row in rows if row.error)
        raise RuntimeError(f"no Speechmatics API key usage available: {errors or 'no keys configured'}")
    return min(available, key=_key_usage_score)


def parse_speechmatics_usage(payload: dict[str, Any], *, limit_hours: float = 50.0) -> SpeechmaticsUsage:
    summary = payload.get("summary")
    if not isinstance(summary, list):
        summary = []

    transcription_rows = [
        row
        for row in summary
        if isinstance(row, dict) and str(row.get("type", "")).strip().lower() == "transcription"
    ]
    rows = transcription_rows or [row for row in summary if isinstance(row, dict)]

    used_hours = sum(_float(row.get("duration_hrs")) for row in rows)
    normalized_limit_hours = max(0.0, limit_hours)
    percent_used = (used_hours / normalized_limit_hours * 100.0) if normalized_limit_hours > 0 else None
    job_count = sum(int(_float(row.get("count"))) for row in rows)

    return SpeechmaticsUsage(
        used_hours=used_hours,
        limit_hours=normalized_limit_hours,
        percent_used=percent_used,
        job_count=job_count,
        since=str(payload.get("since", "") or ""),
        until=str(payload.get("until", "") or ""),
    )


def format_speechmatics_usage(usage: SpeechmaticsUsage) -> str:
    parts = ["speechmatics usage"]
    if usage.percent_used is not None and usage.limit_hours > 0:
        parts.append(_format_percent(usage.percent_used))
        parts.append(f"{_format_hours(usage.used_hours)}/{_format_hours(usage.limit_hours)}")
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


def _key_usage_score(row: SpeechmaticsKeyUsage) -> tuple[float, float, str]:
    if row.usage is None:
        return (float("inf"), float("inf"), row.key.name)
    percent = row.usage.percent_used if row.usage.percent_used is not None else row.usage.used_hours
    return (percent, row.usage.used_hours, row.key.name)


def _float(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return 0.0
    return 0.0


def _format_hours(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".") + "h"


def _format_percent(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".") + "%"
