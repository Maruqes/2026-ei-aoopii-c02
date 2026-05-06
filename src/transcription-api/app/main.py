from __future__ import annotations

import logging
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, status

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.repository import DataRepository, MessageInsert  # noqa: E402

from .config import Settings
from .schemas import TranscriptionAcceptedResponse
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

logger = logging.getLogger("uvicorn.error")


def create_app() -> FastAPI:
    service = FastAPI(title="Discord Anthropologist Transcription API")

    @service.get("/health")
    def health(repository: DataRepository = Depends(get_repository)) -> dict[str, str]:
        try:
            repository.healthcheck()
        except Exception as exc:
            logger.exception("healthcheck falhou: database indisponivel")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Database unavailable: {exc}",
            ) from exc
        return {"status": "ok"}

    @service.post("/v1/transcriptions", response_model=TranscriptionAcceptedResponse)
    async def create_transcription(
        recording_filename: str = Form(...),
        discord_id: str = Form(...),
        username: str = Form(...),
        channel_name: str = Form(...),
        recording_started_at: datetime = Form(...),
        display_name: str | None = Form(None),
        settings: Settings = Depends(get_settings),
        repository: DataRepository = Depends(get_repository),
        transcriber: WhisperTranscriber = Depends(get_transcriber),
    ) -> TranscriptionAcceptedResponse:
        started = time.perf_counter()
        logger.info(
            "API /v1/transcriptions recebida recording_filename=%s discord_id=%s username=%s channel=%s started_at=%s",
            recording_filename,
            discord_id,
            username,
            channel_name,
            recording_started_at.isoformat(),
        )

        try:
            validate_metadata(discord_id, username, channel_name)
            validate_recording_filename(recording_filename)
            recording_path = resolve_recording_path(recording_filename, settings)
            validate_upload_name(recording_path.name)
            validate_recording_file(recording_path, settings)

            schedule_transcription_job(
                recording_path=recording_path,
                discord_id=discord_id.strip(),
                username=username.strip(),
                display_name=display_name.strip() if display_name else None,
                channel_name=channel_name.strip(),
                recording_started_at=recording_started_at,
                settings=settings,
                repository=repository,
                transcriber=transcriber,
            )
        except HTTPException as exc:
            logger.warning(
                "API /v1/transcriptions rejeitada recording_filename=%s discord_id=%s status=%s detail=%s elapsed_ms=%d",
                recording_filename,
                discord_id,
                exc.status_code,
                exc.detail,
                int((time.perf_counter() - started) * 1000),
            )
            raise
        except Exception as exc:
            logger.exception(
                "API /v1/transcriptions erro recording_filename=%s discord_id=%s elapsed_ms=%d",
                recording_filename,
                discord_id,
                int((time.perf_counter() - started) * 1000),
            )
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc

        processing_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "API /v1/transcriptions aceite discord_id=%s recording_filename=%s processing_ms=%d",
            discord_id.strip(),
            recording_path.name,
            processing_ms,
        )

        return TranscriptionAcceptedResponse(
            status="accepted",
            recording_filename=recording_path.name,
            message="Transcription scheduled",
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


def validate_recording_filename(recording_filename: str) -> None:
    value = recording_filename.strip()
    if not value:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Missing required metadata: recording_filename",
        )

    if "/" in value or "\\" in value or value in {".", ".."} or Path(value).name != value:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="recording_filename must be a file name inside the shared recordings folder",
        )


def resolve_recording_path(recording_filename: str, settings: Settings) -> Path:
    recordings_dir = settings.recordings_dir.resolve()
    recording_path = (recordings_dir / recording_filename.strip()).resolve()
    if recording_path.parent != recordings_dir:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="recording_filename must resolve inside the shared recordings folder",
        )
    return recording_path


def validate_recording_file(recording_path: Path, settings: Settings) -> None:
    if not recording_path.exists() or not recording_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Recording file not found: {recording_path.name}",
        )

    size = recording_path.stat().st_size
    if size == 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Recording file is empty")
    if size > settings.max_upload_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Recording exceeds {settings.max_upload_bytes} bytes",
        )


def schedule_transcription_job(
    *,
    recording_path: Path,
    discord_id: str,
    username: str,
    display_name: str | None,
    channel_name: str,
    recording_started_at: datetime,
    settings: Settings,
    repository: DataRepository,
    transcriber: WhisperTranscriber,
) -> None:
    logger.info(
        "job transcricao agendado file=%s discord_id=%s username=%s channel=%s",
        recording_path,
        discord_id,
        username,
        channel_name,
    )
    thread = threading.Thread(
        target=process_recording_file,
        kwargs={
            "recording_path": recording_path,
            "discord_id": discord_id,
            "username": username,
            "display_name": display_name,
            "channel_name": channel_name,
            "recording_started_at": recording_started_at,
            "settings": settings,
            "repository": repository,
            "transcriber": transcriber,
        },
        daemon=True,
    )
    thread.start()


def process_recording_file(
    *,
    recording_path: Path,
    discord_id: str,
    username: str,
    display_name: str | None,
    channel_name: str,
    recording_started_at: datetime,
    settings: Settings,
    repository: DataRepository,
    transcriber: WhisperTranscriber,
) -> None:
    started = time.perf_counter()
    try:
        logger.info(
            "job transcricao inicio file=%s bytes=%d model=%s discord_id=%s",
            recording_path,
            recording_path.stat().st_size,
            settings.whisper_model,
            discord_id,
        )
        whisper_result = transcriber.transcribe(recording_path)
        logger.info(
            "job whisper concluido file=%s discord_id=%s segmentos=%d texto_chars=%d",
            recording_path,
            discord_id,
            len(whisper_result.segments),
            len(whisper_result.text),
        )

        messages = messages_from_segments(recording_started_at, whisper_result)
        logger.info(
            "job escrita DB inicio file=%s discord_id=%s username=%s channel=%s mensagens=%d",
            recording_path,
            discord_id,
            username,
            channel_name,
            len(messages),
        )
        insert_result = repository.insert_transcription_segments(
            discord_id=discord_id,
            username=username,
            display_name=display_name,
            channel_name=channel_name,
            messages=messages,
        )
        logger.info(
            "job escrita DB concluida file=%s discord_id=%s user_id=%s message_ids=%d chunks_afetados=%d elapsed_ms=%d",
            recording_path,
            discord_id,
            insert_result.user_id,
            len(insert_result.message_ids),
            len(insert_result.affected_chunks),
            int((time.perf_counter() - started) * 1000),
        )
    except Exception:
        logger.exception(
            "job transcricao erro file=%s discord_id=%s elapsed_ms=%d",
            recording_path,
            discord_id,
            int((time.perf_counter() - started) * 1000),
        )


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
