import pytest
from app.config import Settings


@pytest.mark.parametrize(
    ("value", "expected"),
    [("60", 60), ("5", 5), ("0", 1), ("-1", 1), ("nan", 60), ("inf", 60)],
)
def test_cleanup_interval_cannot_disable_or_break_periodic_sweep(
    monkeypatch, value, expected
):
    monkeypatch.setenv("RECORDING_CLEANUP_INTERVAL_SECONDS", value)
    assert Settings.from_env().recording_cleanup_interval_seconds == expected
