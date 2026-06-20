from app.speechmatics_usage import (
    SpeechmaticsAPIKey,
    SpeechmaticsKeyUsage,
    format_speechmatics_key_usage,
    format_speechmatics_usage,
    parse_speechmatics_usage,
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
