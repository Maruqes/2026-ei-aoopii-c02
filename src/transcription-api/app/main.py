from __future__ import annotations

import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile, status

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.repository import DataRepository, MessageInsert  # noqa: E402

from .config import Settings
from .schemas import ChunkResponse, TranscriptionResponse, TranscriptSegment
from .transcriber import WhisperResult, WhisperTranscriber


SUPPORTED_EXTENSIONS = {
    ".wav",
    ".mp3",
    ".m4a",
    ".mp4",
    ".mpeg",
    ".mpga",
    ".webm",
    ".ogg",
    ".flac",
}


def create_app() -> FastAPI:
    service = FastAPI(title="Discord Anthropologist Transcription API")

    @service.get("/health")
    def health(repository: DataRepository = Depends(get_repository)) -> dict[str, str]:
        try:
            repository.healthcheck()
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Database unavailable: {exc}",
            ) from exc
        return {"status": "ok"}

    @service.post("/v1/transcriptions", response_model=TranscriptionResponse)
    async def create_transcription(
        audio: UploadFile = File(...),
        discord_id: str = Form(...),
        username: str = Form(...),
        channel_name: str = Form(...),
        recording_started_at: datetime = Form(...),
        display_name: str | None = Form(None),
        settings: Settings = Depends(get_settings),
        repository: DataRepository = Depends(get_repository),
        transcriber: WhisperTranscriber = Depends(get_transcriber),
    ) -> TranscriptionResponse:
        started = time.perf_counter()
        validate_metadata(discord_id, username, channel_name)
        validate_upload_name(audio.filename)

        upload_path = await save_upload(audio, settings)
        try:
            whisper_result = transcriber.transcribe(upload_path)
            messages = messages_from_segments(recording_started_at, whisper_result)
            insert_result = repository.insert_transcription_segments(
                discord_id=discord_id.strip(),
                username=username.strip(),
                display_name=display_name.strip() if display_name else None,
                channel_name=channel_name.strip(),
                messages=messages,
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc
        finally:
            if not settings.keep_uploads:
                upload_path.unlink(missing_ok=True)

        return TranscriptionResponse(
            text=whisper_result.text,
            segments=[
                TranscriptSegment(start=segment.start, end=segment.end, text=segment.text)
                for segment in whisper_result.segments
            ],
            user_id=insert_result.user_id,
            message_ids=insert_result.message_ids,
            affected_chunks=[
                ChunkResponse(
                    id=chunk.id,
                    channel_name=chunk.channel_name,
                    start_at=chunk.start_at,
                    end_at=chunk.end_at,
                )
                for chunk in insert_result.affected_chunks
            ],
            model=settings.whisper_model,
            processing_ms=int((time.perf_counter() - started) * 1000),
        )

    return service


def get_settings() -> Settings:
    return Settings.from_env()


def get_repository(settings: Settings = Depends(get_settings)) -> DataRepository:
    return DataRepository(settings.database_url)


def get_transcriber(settings: Settings = Depends(get_settings)) -> WhisperTranscriber:
    return WhisperTranscriber(settings.whisper_model)


def validate_metadata(discord_id: str, username: str, channel_name: str) -> None:
    missing = [
        name
        for name, value in {
            "discord_id": discord_id,
            "username": username,
            "channel_name": channel_name,
        }.items()
        if not value.strip()
    ]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Missing required metadata: {', '.join(missing)}",
        )


def validate_upload_name(filename: str | None) -> None:
    suffix = Path(filename or "").suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported audio file extension: {suffix or '<none>'}",
        )


async def save_upload(audio: UploadFile, settings: Settings) -> Path:
    settings.upload_tmp_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(audio.filename or "").suffix.lower()
    upload_path = settings.upload_tmp_dir / f"{uuid.uuid4()}{suffix}"
    total = 0

    try:
        with upload_path.open("wb") as out:
            while chunk := await audio.read(1024 * 1024):
                total += len(chunk)
                if total > settings.max_upload_bytes:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"Upload exceeds {settings.max_upload_bytes} bytes",
                    )
                out.write(chunk)
    except Exception:
        upload_path.unlink(missing_ok=True)
        raise
    finally:
        await audio.close()

    if total == 0:
        upload_path.unlink(missing_ok=True)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded audio is empty")

    return upload_path


def messages_from_segments(recording_started_at: datetime, result: WhisperResult) -> list[MessageInsert]:
    started_at = normalize_datetime(recording_started_at)
    return [
        MessageInsert(
            content=segment.text,
            tstamp=started_at + timedelta(seconds=segment.start),
        )
        for segment in result.segments
        if segment.text.strip()
    ]


def normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


app = create_app()
