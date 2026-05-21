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
import app.main as main_module  # noqa: E402
from app.llm import OllamaClient, OpenAICompatibleClient  # noqa: E402
from app.main import create_app, get_llm_client, get_repository, get_settings, get_transcriber  # noqa: E402
from app.transcriber import WhisperResult, WhisperSegment  # noqa: E402
from data.repository import ChunkInfo, TranscriptionInsertResult, VoiceSession  # noqa: E402


class FakeRepository:
    def __init__(self):
        self.calls = []
        self.recordings = []
        self.completed_recordings = []
        self.failed_recordings = []
        self.sessions = {
            42: VoiceSession(
                id=42,
                guild_id="guild-1",
                voice_channel_id="voice-1",
                channel_name="general",
                summary_channel_id="text-1",
                started_at=datetime(2026, 4, 28, 10, 0, tzinfo=timezone.utc),
                ended_at=None,
                status="open",
                summary=None,
                agent_error=None,
            )
        }

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

    def start_recording(self, **kwargs):
        self.recordings.append(kwargs)
        return 99 if kwargs.get("session_id") else None

    def mark_recording_completed(self, recording_id):
        if recording_id is None:
            return
        self.completed_recordings.append(recording_id)

    def mark_recording_failed(self, recording_id, error):
        if recording_id is None:
            return
        self.failed_recordings.append((recording_id, error))

    def create_voice_session(self, **kwargs):
        session = VoiceSession(
            id=43,
            guild_id=kwargs["guild_id"],
            voice_channel_id=kwargs["voice_channel_id"],
            channel_name=kwargs["channel_name"],
            summary_channel_id=kwargs["summary_channel_id"],
            started_at=kwargs["started_at"],
            ended_at=None,
            status="open",
            summary=None,
            agent_error=None,
        )
        self.sessions[session.id] = session
        return session

    def finish_voice_session(self, session_id, ended_at):
        session = self.sessions.get(session_id)
        if not session:
            return None
        finished = VoiceSession(
            id=session.id,
            guild_id=session.guild_id,
            voice_channel_id=session.voice_channel_id,
            channel_name=session.channel_name,
            summary_channel_id=session.summary_channel_id,
            started_at=session.started_at,
            ended_at=ended_at,
            status="finished",
            summary=session.summary,
            agent_error=session.agent_error,
        )
        self.sessions[session_id] = finished
        return finished

    def get_voice_session(self, session_id):
        return self.sessions.get(session_id)

    def get_user_profile_by_discord_id(self, discord_id):
        return None


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
    recordings_dir = tmp_path / "recordings"
    recordings_dir.mkdir()
    app.dependency_overrides[get_settings] = lambda: Settings(
        database_url="postgresql://test:test@localhost:5432/test",
        whisper_model="base",
        max_upload_bytes=max_upload_bytes,
        upload_tmp_dir=tmp_path / "uploads",
        recordings_dir=recordings_dir,
        keep_uploads=False,
    )
    app.dependency_overrides[get_repository] = lambda: fake_repo
    app.dependency_overrides[get_transcriber] = lambda: FakeTranscriber()
    return TestClient(app), fake_repo, recordings_dir


def test_settings_from_env_accepts_openai_compatible_values(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setenv("OPENAI_MODEL", "custom-model")

    settings = Settings.from_env()

    assert settings.llm_provider == "openai"
    assert settings.openai_api_key == "test-key"
    assert settings.openai_base_url == "https://api.example.com/v1"
    assert settings.openai_model == "custom-model"


def test_settings_from_env_falls_back_to_legacy_groq_values(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("OPENAI_BASE_URL", "")
    monkeypatch.setenv("OPENAI_MODEL", "")
    monkeypatch.setenv("GROQ_API_KEY", "groq-key")
    monkeypatch.setenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
    monkeypatch.setenv("GROQ_MODEL", "llama-3.3-70b-versatile")

    settings = Settings.from_env()

    assert settings.openai_api_key == "groq-key"
    assert settings.openai_base_url == "https://api.groq.com/openai/v1"
    assert settings.openai_model == "llama-3.3-70b-versatile"


def test_get_llm_client_uses_openai_compatible_provider():
    client = get_llm_client(
        Settings(
            database_url="postgresql://test:test@localhost:5432/test",
            llm_provider="openai",
            openai_api_key="test-key",
            openai_base_url="https://api.example.com/v1",
            openai_model="custom-model",
        )
    )

    assert isinstance(client, OpenAICompatibleClient)
    assert client.api_key == "test-key"
    assert client.base_url == "https://api.example.com/v1"
    assert client.model == "custom-model"


def test_get_llm_client_accepts_legacy_groq_provider():
    client = get_llm_client(
        Settings(
            database_url="postgresql://test:test@localhost:5432/test",
            llm_provider="groq",
            groq_api_key="test-key",
            groq_base_url="https://api.groq.com/openai/v1",
            groq_model="llama-3.3-70b-versatile",
        )
    )

    assert isinstance(client, OpenAICompatibleClient)
    assert client.api_key == "test-key"
    assert client.base_url == "https://api.groq.com/openai/v1"
    assert client.model == "llama-3.3-70b-versatile"


def test_get_llm_client_uses_ollama_provider():
    client = get_llm_client(
        Settings(
            database_url="postgresql://test:test@localhost:5432/test",
            llm_provider="ollama",
            ollama_base_url="http://localhost:11434",
            ollama_model="qwen2.5:7b",
        )
    )

    assert isinstance(client, OllamaClient)
    assert client.base_url == "http://localhost:11434"
    assert client.model == "qwen2.5:7b"


def test_health_uses_repository(tmp_path):
    client, _, _ = make_client(tmp_path)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_transcription_endpoint_schedules_background_job_and_returns_200(tmp_path, monkeypatch):
    client, _, recordings_dir = make_client(tmp_path)
    recording_path = recordings_dir / "sample.wav"
    recording_path.write_bytes(b"fake wav")
    scheduled = []

    def fake_schedule_transcription_job(**kwargs):
        scheduled.append(kwargs)

    monkeypatch.setattr(main_module, "schedule_transcription_job", fake_schedule_transcription_job)

    response = client.post(
        "/v1/transcriptions",
        data={
            "recording_filename": "sample.wav",
            "discord_id": "123",
            "username": "Ricardo",
            "display_name": "Ricardo F",
            "channel_name": "general",
            "recording_started_at": "2026-04-28T10:03:00Z",
            "session_id": "42",
        },
    )

    body = response.json()

    assert response.status_code == 200
    assert body == {
        "status": "accepted",
        "recording_filename": "sample.wav",
        "message": "Transcription scheduled",
    }
    assert len(scheduled) == 1
    assert scheduled[0]["recording_path"] == recording_path.resolve()
    assert scheduled[0]["session_id"] == 42
    assert scheduled[0]["recording_id"] == 99
    assert scheduled[0]["discord_id"] == "123"
    assert scheduled[0]["channel_name"] == "general"


def test_process_recording_file_transcribes_and_inserts_segments(tmp_path):
    recordings_dir = tmp_path / "recordings"
    recordings_dir.mkdir()
    recording_path = recordings_dir / "sample.wav"
    recording_path.write_bytes(b"fake wav")
    repository = FakeRepository()

    main_module.process_recording_file(
        recording_path=recording_path,
        discord_id="123",
        username="Ricardo",
        display_name="Ricardo F",
        channel_name="general",
        recording_started_at=datetime(2026, 4, 28, 10, 3, tzinfo=timezone.utc),
        settings=Settings(
            database_url="postgresql://test:test@localhost:5432/test",
            whisper_model="base",
            recordings_dir=recordings_dir,
        ),
        repository=repository,
        transcriber=FakeTranscriber(),
    )

    inserted_messages = repository.calls[0]["messages"]
    assert repository.completed_recordings == []
    assert repository.calls[0]["discord_id"] == "123"
    assert repository.calls[0]["channel_name"] == "general"
    assert inserted_messages[0].content == "hello"
    assert inserted_messages[0].tstamp == datetime(2026, 4, 28, 10, 3, 1, tzinfo=timezone.utc)
    assert inserted_messages[1].tstamp == datetime(2026, 4, 28, 10, 4, 2, tzinfo=timezone.utc)


def test_create_session_returns_session_metadata(tmp_path):
    client, _, _ = make_client(tmp_path)

    response = client.post(
        "/v1/sessions",
        json={
            "guild_id": "guild-1",
            "voice_channel_id": "voice-1",
            "channel_name": "general",
            "summary_channel_id": "text-1",
            "started_at": "2026-04-28T10:00:00Z",
        },
    )

    body = response.json()

    assert response.status_code == 200
    assert body["id"] == 43
    assert body["guild_id"] == "guild-1"
    assert body["status"] == "open"


def test_get_session_summary_returns_current_status(tmp_path):
    client, _, _ = make_client(tmp_path)

    response = client.get("/v1/sessions/42/summary")

    assert response.status_code == 200
    assert response.json()["status"] == "open"


def test_transcription_rejects_unsupported_extension(tmp_path):
    client, _, _ = make_client(tmp_path)

    response = client.post(
        "/v1/transcriptions",
        data={
            "recording_filename": "sample.txt",
            "discord_id": "123",
            "username": "Ricardo",
            "channel_name": "general",
            "recording_started_at": "2026-04-28T10:03:00Z",
        },
    )

    assert response.status_code == 415


def test_transcription_rejects_oversized_upload(tmp_path):
    client, _, recordings_dir = make_client(tmp_path, max_upload_bytes=4)
    (recordings_dir / "sample.wav").write_bytes(b"too large")

    response = client.post(
        "/v1/transcriptions",
        data={
            "recording_filename": "sample.wav",
            "discord_id": "123",
            "username": "Ricardo",
            "channel_name": "general",
            "recording_started_at": "2026-04-28T10:03:00Z",
        },
    )

    assert response.status_code == 413


def test_transcription_rejects_recording_filename_outside_recordings_dir(tmp_path):
    client, _, _ = make_client(tmp_path)

    response = client.post(
        "/v1/transcriptions",
        data={
            "recording_filename": "../sample.wav",
            "discord_id": "123",
            "username": "Ricardo",
            "channel_name": "general",
            "recording_started_at": "2026-04-28T10:03:00Z",
        },
    )

    assert response.status_code == 422
