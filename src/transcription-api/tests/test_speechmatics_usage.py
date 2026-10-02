from app.speechmatics_usage import (
    SpeechmaticsAPIKey,
    SpeechmaticsKeyUsage,
    SpeechmaticsUsage,
    format_speechmatics_key_usage,
    format_speechmatics_usage,
    parse_speechmatics_usage,
    select_speechmatics_api_key,
    speechmatics_key_usage_score,
)


def test_formats_speechmatics_usage_with_limit() -> None:
    usage = parse_speechmatics_usage(
        {
            "since": "2026-06-01T00:00:00Z",
            "until": "2026-06-20T23:59:59Z",
            "summary": [
                {
                    "mode": "batch",
                    "type": "transcription",
                    "count": 5,
                    "duration_hrs": 10,
                },
                {"mode": "batch", "type": "alignment", "count": 1, "duration_hrs": 3},
            ],
            "details": [],
        },
        limit_hours=50,
    )

    assert usage.used_hours == 10
    assert usage.limit_hours == 50
    assert usage.percent_used == 20
    assert usage.job_count == 5
    assert format_speechmatics_usage(usage) == (
        "speechmatics usage 20% 10h/50h jobs=5 since=2026-06-01T00:00:00Z "
        "until=2026-06-20T23:59:59Z"
    )


def test_formats_speechmatics_usage_without_limit() -> None:
    usage = parse_speechmatics_usage(
        {
            "summary": [
                {
                    "mode": "batch",
                    "type": "transcription",
                    "count": 2,
                    "duration_hrs": 1.5,
                }
            ]
        },
        limit_hours=0,
    )

    assert format_speechmatics_usage(usage) == "speechmatics usage current=1.5h jobs=2"


def test_formats_key_usage() -> None:
    row = SpeechmaticsKeyUsage(
        key=SpeechmaticsAPIKey(name="SPEECHMATICS_API_KEY_03", value="secret"),
        usage=parse_speechmatics_usage(
            {
                "summary": [
                    {
                        "mode": "batch",
                        "type": "transcription",
                        "count": 1,
                        "duration_hrs": 0.5,
                    }
                ]
            },
            limit_hours=50,
        ),
    )

    assert (
        format_speechmatics_key_usage(row)
        == "SPEECHMATICS_API_KEY_03: 1% 0.5h/50h jobs=1"
    )


def test_selects_key_with_lowest_percent_used(monkeypatch) -> None:
    usages = {
        "key-01": SpeechmaticsUsage(
            used_hours=7 / 60,
            limit_hours=50,
            percent_used=(7 / 60) / 50 * 100,
            job_count=3,
            since="",
            until="",
        ),
        "key-02": SpeechmaticsUsage(
            used_hours=0,
            limit_hours=50,
            percent_used=0,
            job_count=0,
            since="",
            until="",
        ),
        "key-03": SpeechmaticsUsage(
            used_hours=0,
            limit_hours=50,
            percent_used=0,
            job_count=0,
            since="",
            until="",
        ),
    }

    def fake_fetch_speechmatics_usage(**kwargs):
        return usages[kwargs["api_key"]]

    monkeypatch.setattr(
        "app.speechmatics_usage.fetch_speechmatics_usage",
        fake_fetch_speechmatics_usage,
    )

    selected = select_speechmatics_api_key(
        api_keys=(
            SpeechmaticsAPIKey(name="SPEECHMATICS_API_KEY_01", value="key-01"),
            SpeechmaticsAPIKey(name="SPEECHMATICS_API_KEY_02", value="key-02"),
            SpeechmaticsAPIKey(name="SPEECHMATICS_API_KEY_03", value="key-03"),
        ),
        batch_url="https://example.invalid",
        limit_hours=50,
    )

    assert selected.key.name == "SPEECHMATICS_API_KEY_02"


def test_speechmatics_key_usage_score_prefers_percent_before_name() -> None:
    row_01 = SpeechmaticsKeyUsage(
        key=SpeechmaticsAPIKey(name="SPEECHMATICS_API_KEY_01", value="key-01"),
        usage=SpeechmaticsUsage(
            used_hours=7 / 60,
            limit_hours=50,
            percent_used=(7 / 60) / 50 * 100,
            job_count=3,
            since="",
            until="",
        ),
    )
    row_02 = SpeechmaticsKeyUsage(
        key=SpeechmaticsAPIKey(name="SPEECHMATICS_API_KEY_02", value="key-02"),
        usage=SpeechmaticsUsage(
            used_hours=0,
            limit_hours=50,
            percent_used=0,
            job_count=0,
            since="",
            until="",
        ),
    )

    assert (
        min((row_01, row_02), key=speechmatics_key_usage_score).key.name
        == "SPEECHMATICS_API_KEY_02"
    )


def test_alignment_and_realtime_are_not_counted_as_batch_transcription():
    usage = parse_speechmatics_usage(
        {
            "summary": [
                {"mode": "batch", "type": "alignment", "duration_hrs": 9, "count": 10},
                {
                    "mode": "realtime",
                    "type": "transcription",
                    "duration_hrs": 20,
                    "count": 5,
                },
            ]
        },
        limit_hours=50,
    )
    assert usage.used_hours == 0
    assert usage.percent_used == 0


def test_details_fallback_without_double_counting_summary():
    row = {
        "mode": "batch",
        "type": "transcription",
        "duration_hrs": "1.25",
        "count": "3",
    }
    assert (
        parse_speechmatics_usage({"summary": [row], "details": [row]}).used_hours
        == 1.25
    )
    assert (
        parse_speechmatics_usage({"summary": None, "details": [row]}).used_hours == 1.25
    )
    assert parse_speechmatics_usage({"summary": None, "details": None}).used_hours == 0


def test_unknown_or_malformed_usage_is_not_reported_as_zero():
    import pytest

    for payload in (
        {},
        {"summary": {}},
        {"summary": [{"type": "transcription", "duration_hrs": "NaN", "count": 1}]},
        {"summary": [{"type": "transcription", "duration_hrs": -1, "count": 1}]},
    ):
        with pytest.raises(ValueError):
            parse_speechmatics_usage(payload)


def test_no_percentage_without_an_explicit_budget():
    usage = parse_speechmatics_usage(
        {"summary": [{"type": "transcription", "duration_hrs": 5, "count": 1}]}
    )
    assert usage.used_hours == 5
    assert usage.percent_used is None


def test_usage_requests_explicit_utc_period_and_deduplicates_keys(monkeypatch):
    from datetime import datetime, timedelta, timezone

    import httpx
    from app.speechmatics_usage import fetch_speechmatics_key_usages

    calls = []

    def get(url, **kwargs):
        calls.append(kwargs)
        return httpx.Response(
            200, json={"summary": None}, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr("app.speechmatics_usage.httpx.get", get)
    rows = fetch_speechmatics_key_usages(
        api_keys=(SpeechmaticsAPIKey("a", "same"), SpeechmaticsAPIKey("b", "same")),
        batch_url="https://example.invalid",
        since="2026-01-01",
    )
    assert len(rows) == 1
    assert len(calls) == 1
    assert calls[0]["params"] == {
        "since": "2026-01-01",
        "until": (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat(),
    }


def test_usage_errors_never_include_provider_echoed_secret(monkeypatch):
    import httpx
    from app.speechmatics_usage import fetch_speechmatics_key_usages

    def get(url, **kwargs):
        return httpx.Response(
            401, text="echoed-secret", request=httpx.Request("GET", url)
        )

    monkeypatch.setattr("app.speechmatics_usage.httpx.get", get)
    row = fetch_speechmatics_key_usages(
        api_keys=(SpeechmaticsAPIKey("a", "secret"),),
        batch_url="https://example.invalid",
        since="2026-01-01",
    )[0]
    assert row.error == "HTTP 401"
    assert row.usage is None
