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
                {"mode": "batch", "type": "transcription", "count": 5, "duration_hrs": 10},
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
        {"summary": [{"mode": "batch", "type": "transcription", "count": 2, "duration_hrs": 1.5}]},
        limit_hours=0,
    )

    assert format_speechmatics_usage(usage) == "speechmatics usage current=1.5h jobs=2"


def test_formats_key_usage() -> None:
    row = SpeechmaticsKeyUsage(
        key=SpeechmaticsAPIKey(name="SPEECHMATICS_API_KEY_03", value="secret"),
        usage=parse_speechmatics_usage(
            {"summary": [{"mode": "batch", "type": "transcription", "count": 1, "duration_hrs": 0.5}]},
            limit_hours=50,
        ),
    )

    assert format_speechmatics_key_usage(row) == "SPEECHMATICS_API_KEY_03: 1% 0.5h/50h jobs=1"


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

    assert min((row_01, row_02), key=speechmatics_key_usage_score).key.name == "SPEECHMATICS_API_KEY_02"
