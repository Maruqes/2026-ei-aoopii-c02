from __future__ import annotations

import asyncio
import sys
import types

from app.speechmatics_usage import SpeechmaticsAPIKey
from app.transcriber import SpeechmaticsTranscriber


def test_transcriber_passes_selected_key_to_batch_client(monkeypatch, tmp_path) -> None:
    opened_clients: list[dict[str, str]] = []

    class FakeTranscript:
        results = []

    class FakeTranscriptionConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            opened_clients.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def transcribe(self, *args, **kwargs):
            return FakeTranscript()

    fake_batch = types.ModuleType("speechmatics.batch")
    fake_batch.AsyncClient = FakeAsyncClient
    fake_batch.Transcript = FakeTranscript
    fake_batch.TranscriptionConfig = FakeTranscriptionConfig
    fake_speechmatics = types.ModuleType("speechmatics")
    fake_speechmatics.batch = fake_batch
    monkeypatch.setitem(sys.modules, "speechmatics", fake_speechmatics)
    monkeypatch.setitem(sys.modules, "speechmatics.batch", fake_batch)

    transcriber = SpeechmaticsTranscriber(
        "",
        api_keys=(SpeechmaticsAPIKey(name="SPEECHMATICS_API_KEY_02", value="key-02"),),
        batch_url="https://example.invalid/v2",
    )

    result = asyncio.run(
        transcriber._transcribe(
            tmp_path / "recording.wav",
            SpeechmaticsAPIKey(name="SPEECHMATICS_API_KEY_02", value="key-02"),
        )
    )

    assert result.text == ""
    assert opened_clients == [{"api_key": "key-02", "url": "https://example.invalid/v2"}]
