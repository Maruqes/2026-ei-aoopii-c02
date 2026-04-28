from datetime import datetime, timezone

from data.repository import affected_windows, chunk_window_for_timestamp, format_chunk_rows


def test_chunk_window_uses_fixed_30_minute_boundaries():
    start, end = chunk_window_for_timestamp(datetime(2026, 4, 28, 10, 42, 10, tzinfo=timezone.utc))

    assert start == datetime(2026, 4, 28, 10, 30, tzinfo=timezone.utc)
    assert end == datetime(2026, 4, 28, 11, 0, tzinfo=timezone.utc)


def test_affected_windows_deduplicates_same_window_and_orders_results():
    windows = affected_windows(
        [
            datetime(2026, 4, 28, 11, 3, tzinfo=timezone.utc),
            datetime(2026, 4, 28, 10, 5, tzinfo=timezone.utc),
            datetime(2026, 4, 28, 10, 29, tzinfo=timezone.utc),
        ]
    )

    assert windows == [
        (
            datetime(2026, 4, 28, 10, 0, tzinfo=timezone.utc),
            datetime(2026, 4, 28, 10, 30, tzinfo=timezone.utc),
        ),
        (
            datetime(2026, 4, 28, 11, 0, tzinfo=timezone.utc),
            datetime(2026, 4, 28, 11, 30, tzinfo=timezone.utc),
        ),
    ]


def test_format_chunk_rows_keeps_timestamp_order_format():
    content = format_chunk_rows(
        [
            {
                "tstamp": datetime(2026, 4, 28, 10, 3, tzinfo=timezone.utc),
                "username": "Ricardo",
                "content": "  hello   world ",
            },
            {
                "tstamp": datetime(2026, 4, 28, 10, 4, tzinfo=timezone.utc),
                "username": "Goncalo",
                "content": "testing whisper",
            },
        ]
    )

    assert content == "[10:03] Ricardo: hello world\n[10:04] Goncalo: testing whisper"
