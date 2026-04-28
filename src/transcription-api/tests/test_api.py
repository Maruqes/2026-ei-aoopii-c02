from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
API_ROOT = ROOT / "transcription-api"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from app.config import Settings  # noqa: E402
from app.main import create_app, get_repository, get_settings, get_transcriber  # noqa: E402
from app.transcriber import WhisperResult, WhisperSegment  # noqa: E402
from data.repository import ChunkInfo, TranscriptionInsertResult  # noqa: E402


class FakeRepository:
    def __init__(self):
        self.calls = []

    def healthcheck(self) -> bool:
        return True

    def insert_transcription_segments(self, **kwargs):
        self.calls.append(kwargs)
        return TranscriptionInsertResult(
            user_id=7,
            message_ids=[101, 102],
            affected_chunks=[
                ChunkInfo(
                    id=55,
                    channel_name=kwargs["channel_name"],
                    start_at=datetime(2026, 4, 28, 10, 0, tzinfo=timezone.utc),
                    end_at=datetime(2026, 4, 28, 10, 30, tzinfo=timezone.utc),
                )
            ],
        )


class FakeTranscriber:
    def transcribe(self, audio_path):
        assert audio_path.exists()
        return WhisperResult(
            text="hello testing",
            segments=[
                WhisperSegment(start=1.0, end=2.5, text="hello"),
                WhisperSegment(start=62.0, end=65.0, text="testing"),
            ],
        )


def make_client(tmp_path, repository=None, max_upload_bytes=1024 * 1024):
    app = create_app()
    fake_repo = repository or FakeRepository()
    app.dependency_overrides[get_settings] = lambda: Settings(
        database_url="postgresql://test:test@localhost:5432/test",
        whisper_model="base",
        max_upload_bytes=max_upload_bytes,
        upload_tmp_dir=tmp_path,
        keep_uploads=False,
    )
    app.dependency_overrides[get_repository] = lambda: fake_repo
    app.dependency_overrides[get_transcriber] = lambda: FakeTranscriber()
    return TestClient(app), fake_repo


def test_health_uses_repository(tmp_path):
    client, _ = make_client(tmp_path)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_transcription_endpoint_inserts_segments_and_returns_chunks(tmp_path):
    client, repository = make_client(tmp_path)

    response = client.post(
        "/v1/transcriptions",
        data={
            "discord_id": "123",
            "username": "Ricardo",
            "display_name": "Ricardo F",
            "channel_name": "general",
            "recording_started_at": "2026-04-28T10:03:00Z",
        },
        files={"audio": ("sample.wav", b"not real wav but transcriber is mocked", "audio/wav")},
    )

    body = response.json()

    assert response.status_code == 200
    assert body["text"] == "hello testing"
    assert body["message_ids"] == [101, 102]
    assert body["affected_chunks"][0]["channel_name"] == "general"
    inserted_messages = repository.calls[0]["messages"]
    assert inserted_messages[0].content == "hello"
    assert inserted_messages[0].tstamp == datetime(2026, 4, 28, 10, 3, 1, tzinfo=timezone.utc)
    assert inserted_messages[1].tstamp == datetime(2026, 4, 28, 10, 4, 2, tzinfo=timezone.utc)


def test_transcription_rejects_unsupported_extension(tmp_path):
    client, _ = make_client(tmp_path)

    response = client.post(
        "/v1/transcriptions",
        data={
            "discord_id": "123",
            "username": "Ricardo",
            "channel_name": "general",
            "recording_started_at": "2026-04-28T10:03:00Z",
        },
        files={"audio": ("sample.txt", b"audio", "text/plain")},
    )

    assert response.status_code == 415


def test_transcription_rejects_oversized_upload(tmp_path):
    client, _ = make_client(tmp_path, max_upload_bytes=4)

    response = client.post(
        "/v1/transcriptions",
        data={
            "discord_id": "123",
            "username": "Ricardo",
            "channel_name": "general",
            "recording_started_at": "2026-04-28T10:03:00Z",
        },
        files={"audio": ("sample.wav", b"too large", "audio/wav")},
    )

    assert response.status_code == 413
