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

from data.repository import DataRepository, MessageInsert, UserProfile, VoiceSession  # noqa: E402

from .agent import SessionAgent
from .config import Settings
from .docs_client import LocalMarkdownProfileClient
from .llm import LLMClient, OllamaClient, OpenAICompatibleClient
from .profile_updater import run_text_profile_sync, start_text_profile_sync_loop
from .schemas import (
    CreateSessionRequest,
    FinishSessionRequest,
    SessionSummaryResponse,
    TextMessageRequest,
    TextMessageResponse,
    TextProfileSyncResponse,
    TranscriptionAcceptedResponse,
    UserProfileResponse,
    VoiceSessionResponse,
)
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

    @service.on_event("startup")
    def start_profile_sync() -> None:
        settings = get_settings()
        if not settings.text_profile_sync_enabled:
            return
        start_text_profile_sync_loop(
            repository=DataRepository(settings.database_url),
            llm=get_llm_client(settings),
            docs=get_docs_client(settings),
            interval_hours=settings.text_profile_sync_interval_hours,
        )

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
        session_id: int | None = Form(None),
        display_name: str | None = Form(None),
        settings: Settings = Depends(get_settings),
        repository: DataRepository = Depends(get_repository),
        transcriber: WhisperTranscriber = Depends(get_transcriber),
        llm: LLMClient = Depends(get_llm_client),
        docs: LocalMarkdownProfileClient = Depends(get_docs_client),
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
            recording_id = repository.start_recording(
                session_id=session_id,
                recording_filename=recording_path.name,
                discord_id=discord_id.strip(),
            )

            schedule_transcription_job(
                recording_path=recording_path,
                session_id=session_id,
                recording_id=recording_id,
                discord_id=discord_id.strip(),
                username=username.strip(),
                display_name=display_name.strip() if display_name else None,
                channel_name=channel_name.strip(),
                recording_started_at=recording_started_at,
                settings=settings,
                repository=repository,
                transcriber=transcriber,
                llm=llm,
                docs=docs,
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

    @service.post("/v1/messages", response_model=TextMessageResponse)
    def create_text_message(
        request: TextMessageRequest,
        repository: DataRepository = Depends(get_repository),
    ) -> TextMessageResponse:
        validate_text_message(request)
        insert_result = repository.insert_text_message(
            guild_id=request.guild_id.strip(),
            channel_id=request.channel_id.strip(),
            channel_name=request.channel_name.strip(),
            discord_message_id=request.discord_message_id.strip(),
            discord_id=request.discord_id.strip(),
            username=request.username.strip(),
            display_name=request.display_name.strip() if request.display_name else None,
            content=request.content.strip(),
            tstamp=request.tstamp,
            edited_at=request.edited_at,
        )
        return TextMessageResponse(
            status="stored",
            user_id=insert_result.user_id,
            message_id=insert_result.message_id,
        )

    @service.post("/v1/text-profile-sync", response_model=TextProfileSyncResponse)
    def force_text_profile_sync(
        repository: DataRepository = Depends(get_repository),
        llm: LLMClient = Depends(get_llm_client),
        docs: LocalMarkdownProfileClient = Depends(get_docs_client),
    ) -> TextProfileSyncResponse:
        started = time.perf_counter()
        updated = run_text_profile_sync(repository=repository, llm=llm, docs=docs)
        return TextProfileSyncResponse(
            status="completed",
            updated_profiles=updated,
            processing_ms=int((time.perf_counter() - started) * 1000),
        )

    @service.post("/v1/sessions", response_model=VoiceSessionResponse)
    def create_session(
        request: CreateSessionRequest,
        repository: DataRepository = Depends(get_repository),
    ) -> VoiceSessionResponse:
        validate_metadata(request.guild_id, request.voice_channel_id, request.channel_name)
        session = repository.create_voice_session(
            guild_id=request.guild_id,
            voice_channel_id=request.voice_channel_id,
            channel_name=request.channel_name,
            summary_channel_id=request.summary_channel_id,
            started_at=request.started_at or datetime.now(timezone.utc),
        )
        return voice_session_response(session)

    @service.post("/v1/sessions/{session_id}/finish", response_model=VoiceSessionResponse)
    def finish_session(
        session_id: int,
        request: FinishSessionRequest,
        repository: DataRepository = Depends(get_repository),
        llm: LLMClient = Depends(get_llm_client),
        docs: LocalMarkdownProfileClient = Depends(get_docs_client),
    ) -> VoiceSessionResponse:
        session = repository.finish_voice_session(session_id, request.ended_at or datetime.now(timezone.utc))
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
        maybe_schedule_session_agent(session_id, repository, llm, docs)
        return voice_session_response(session)

    @service.get("/v1/sessions/{session_id}/summary", response_model=SessionSummaryResponse)
    def get_session_summary(
        session_id: int,
        repository: DataRepository = Depends(get_repository),
    ) -> SessionSummaryResponse:
        session = repository.get_voice_session(session_id)
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
        return SessionSummaryResponse(
            session_id=session.id,
            status=session.status,
            summary=session.summary,
            agent_error=session.agent_error,
        )

    @service.get("/v1/users/{discord_id}/profile", response_model=UserProfileResponse)
    def get_user_profile(
        discord_id: str,
        repository: DataRepository = Depends(get_repository),
    ) -> UserProfileResponse:
        profile = repository.get_user_profile_by_discord_id(discord_id)
        if profile is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        return user_profile_response(profile)

    return service


def get_settings() -> Settings:
    return Settings.from_env()


def get_repository(settings: Settings = Depends(get_settings)) -> DataRepository:
    return DataRepository(settings.database_url)


def get_transcriber(settings: Settings = Depends(get_settings)) -> WhisperTranscriber:
    return WhisperTranscriber(
        settings.whisper_model,
        settings.whisper_device,
        language=settings.whisper_language,
        beam_size=settings.whisper_beam_size,
        initial_prompt=settings.whisper_initial_prompt,
        carry_initial_prompt=settings.whisper_carry_initial_prompt,
        condition_on_previous_text=settings.whisper_condition_on_previous_text,
    )


def get_llm_client(settings: Settings = Depends(get_settings)) -> LLMClient:
    if settings.llm_provider == "ollama":
        return OllamaClient(base_url=settings.ollama_base_url, model=settings.ollama_model)
    if settings.llm_provider == "openai":
        return OpenAICompatibleClient(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            model=settings.openai_model,
        )
    if settings.llm_provider == "groq":
        return OpenAICompatibleClient(
            api_key=settings.groq_api_key,
            base_url=settings.groq_base_url,
            model=settings.groq_model,
            api_key_env="GROQ_API_KEY",
            provider_name="groq",
        )
    raise RuntimeError(f"Unsupported LLM_PROVIDER: {settings.llm_provider}. Use 'openai', 'groq', or 'ollama'.")


def get_docs_client(settings: Settings = Depends(get_settings)):
    if settings.profile_docs_provider != "local":
        raise RuntimeError(f"Unsupported PROFILE_DOCS_PROVIDER: {settings.profile_docs_provider}")
    return LocalMarkdownProfileClient(profile_dir=settings.local_profile_dir)


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


def validate_text_message(request: TextMessageRequest) -> None:
    missing = [
        name
        for name, value in {
            "guild_id": request.guild_id,
            "channel_id": request.channel_id,
            "channel_name": request.channel_name,
            "discord_message_id": request.discord_message_id,
            "discord_id": request.discord_id,
            "username": request.username,
            "content": request.content,
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

    if recording_path.stat().st_size == 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Recording file is empty")


def schedule_transcription_job(
    *,
    recording_path: Path,
    session_id: int | None,
    recording_id: int | None,
    discord_id: str,
    username: str,
    display_name: str | None,
    channel_name: str,
    recording_started_at: datetime,
    settings: Settings,
    repository: DataRepository,
    transcriber: WhisperTranscriber,
    llm: LLMClient,
    docs: LocalMarkdownProfileClient,
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
            "session_id": session_id,
            "recording_id": recording_id,
            "discord_id": discord_id,
            "username": username,
            "display_name": display_name,
            "channel_name": channel_name,
            "recording_started_at": recording_started_at,
            "settings": settings,
            "repository": repository,
            "transcriber": transcriber,
            "llm": llm,
            "docs": docs,
        },
        daemon=True,
    )
    thread.start()


def process_recording_file(
    *,
    recording_path: Path,
    session_id: int | None = None,
    recording_id: int | None = None,
    discord_id: str,
    username: str,
    display_name: str | None,
    channel_name: str,
    recording_started_at: datetime,
    settings: Settings,
    repository: DataRepository,
    transcriber: WhisperTranscriber,
    llm: LLMClient | None = None,
    docs: LocalMarkdownProfileClient | None = None,
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
            session_id=session_id,
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
        repository.mark_recording_completed(recording_id)
    except Exception:
        repository.mark_recording_failed(recording_id, "transcription failed")
        logger.exception(
            "job transcricao erro file=%s discord_id=%s elapsed_ms=%d",
            recording_path,
            discord_id,
            int((time.perf_counter() - started) * 1000),
        )
    finally:
        if session_id is not None and llm is not None and docs is not None:
            maybe_schedule_session_agent(session_id, repository, llm, docs)


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


def maybe_schedule_session_agent(
    session_id: int,
    repository: DataRepository,
    llm: LLMClient,
    docs: LocalMarkdownProfileClient,
) -> None:
    thread = threading.Thread(
        target=process_session_agent,
        kwargs={
            "session_id": session_id,
            "repository": repository,
            "llm": llm,
            "docs": docs,
        },
        daemon=True,
    )
    thread.start()


def process_session_agent(
    *,
    session_id: int,
    repository: DataRepository,
    llm: LLMClient,
    docs: LocalMarkdownProfileClient,
) -> None:
    try:
        if not repository.claim_session_agent_run(session_id):
            return
        agent = SessionAgent(repository=repository, llm=llm, docs=docs)
        summary = agent.run_for_session(session_id)
        logger.info("agent concluido session_id=%s summary=%s", session_id, summary)
    except Exception as exc:
        repository.mark_session_agent_failed(session_id, str(exc))
        logger.exception("agent erro session_id=%s", session_id)


def voice_session_response(session: VoiceSession) -> VoiceSessionResponse:
    return VoiceSessionResponse(
        id=session.id,
        guild_id=session.guild_id,
        voice_channel_id=session.voice_channel_id,
        channel_name=session.channel_name,
        summary_channel_id=session.summary_channel_id,
        started_at=session.started_at,
        ended_at=session.ended_at,
        status=session.status,
        summary=session.summary,
        agent_error=session.agent_error,
    )


def user_profile_response(profile: UserProfile) -> UserProfileResponse:
    return UserProfileResponse(
        discord_id=profile.discord_id,
        username=profile.username,
        display_name=profile.display_name,
        summary=profile.summary,
        interests=profile.interests,
        communication_style=profile.communication_style,
        persona_notes=profile.known_facts,
        recent_updates=profile.recent_updates,
        google_doc_url=profile.google_doc_url,
        last_updated_at=profile.last_updated_at,
    )


app = create_app()
