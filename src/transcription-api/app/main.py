from __future__ import annotations

import json
import logging
import sys
import threading
import time
import wave
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.apply_migrations import apply_migrations
from data.repository import (  # noqa: E402
    DataRepository,
    MessageInsert,
    UserProfile,
    VoiceSession,
    format_context_message_row,
    normalize_timestamp,
)

from .agent import SessionAgent
from .chatgpt_auth import get_chatgpt_auth
from .chatgpt_control import require_chatgpt_admin
from .chatgpt_llm import ChatGPTClient
from .config import Settings
from .docs_client import LocalMarkdownProfileClient
from .llm import (
    LLMClient,
    OllamaClient,
    OpenAICompatibleClient,
    normalize_response_language,
)
from .model_selection import (
    REASONING_EFFORTS,
    current_effort,
    current_model,
    select_effort,
    select_model,
)
from .profile_updater import run_text_profile_sync, start_text_profile_sync_loop
from .recording_cleanup import RecordingCleanup, remove_recording_files
from .schemas import (
    CreateSessionRequest,
    FinishSessionRequest,
    ForgetUserResponse,
    GuessResponse,
    GuildOracleRequest,
    GuildOracleResponse,
    HealthResponse,
    LLMEffortResponse,
    LLMModelsResponse,
    ProfilePromptRequest,
    ProfilePromptResponse,
    SelectLLMEffortRequest,
    SelectLLMEffortResponse,
    SelectLLMModelRequest,
    SelectLLMModelResponse,
    SessionRecapResponse,
    SessionSummaryResponse,
    SpeechmaticsKeysResponse,
    SpeechmaticsKeyUsageResponse,
    TextDigestRequest,
    TextDigestResponse,
    TextMessageRequest,
    TextMessageResponse,
    TextProfileSyncResponse,
    TranscriptionAcceptedResponse,
    UserProfileResponse,
    VoiceSessionResponse,
)
from .speechmatics_errors import NoCredits, key_health
from .speechmatics_usage import (
    SpeechmaticsAPIKey,
    SpeechmaticsKeyUsage,
    fetch_speechmatics_key_usages,
    speechmatics_key_usage_score,
)
from .streaming_routes import install_streaming_routes
from .transcriber import (
    SpeechmaticsTranscriber,
    Transcriber,
    TranscriptionResult,
    WhisperTranscriber,
)
from .workers import RecordingWorkers

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

_session_agent_lock = threading.Lock()
_session_agent_pending: set[int] = set()


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(service):
        start_profile_sync()
        try:
            yield
        finally:
            stop_workers()

    service = FastAPI(
        title="Discord Anthropologist Transcription API", lifespan=lifespan
    )

    def start_profile_sync() -> None:
        settings = get_settings()
        apply_migrations(settings.database_url)
        repository = DataRepository(settings.database_url)
        repository.recover_realtime_units()
        workers = RecordingWorkers(
            repository=repository,
            settings=settings,
            transcriber_factory=lambda: get_transcriber(settings),
            process_recording=process_recording_file,
        )
        service.state.recording_workers = workers
        cleanup = RecordingCleanup(repository=repository, settings=settings)
        service.state.recording_cleanup = cleanup
        cleanup.start()
        workers.start()
        stop = threading.Event()
        service.state.recovery_stop = stop

        def recover_sessions() -> None:
            while not stop.is_set():
                try:
                    for session_id in repository.get_unfinished_session_ids():
                        maybe_schedule_session_agent(
                            session_id,
                            repository,
                            get_llm_client(settings),
                            get_docs_client(settings),
                        )
                except Exception:
                    logger.exception("Session recovery tick failed")
                stop.wait(3)

        threading.Thread(
            target=recover_sessions, name="session-recovery", daemon=True
        ).start()

        def update_voice_profiles() -> None:
            while not stop.is_set():
                try:
                    for session_id, user_id in repository.get_voice_profile_jobs():
                        if stop.is_set():
                            return
                        with repository.job_lock(103, user_id) as locked:
                            if not locked:
                                continue
                            # Refresh after taking the lock to skip stale queue snapshots.
                            revision = repository.get_voice_profile_job_revision(
                                session_id, user_id
                            )
                            if revision is None:
                                continue
                            try:
                                SessionAgent(
                                    repository=repository,
                                    llm=get_llm_client(settings),
                                    docs=get_docs_client(settings),
                                ).update_participant_profile(session_id, user_id)
                                repository.mark_voice_profile_job(
                                    session_id,
                                    user_id,
                                    "completed",
                                    expected_revision=revision,
                                )
                            except Exception as exc:
                                repository.mark_voice_profile_job(
                                    session_id,
                                    user_id,
                                    "failed",
                                    type(exc).__name__,
                                    expected_revision=revision,
                                )
                                logger.exception(
                                    "Voice profile update failed session=%s user=%s",
                                    session_id,
                                    user_id,
                                )
                except Exception:
                    logger.exception("Voice profile queue unavailable")
                stop.wait(3)

        threading.Thread(
            target=update_voice_profiles, name="voice-profiles", daemon=True
        ).start()
        if not settings.text_profile_sync_enabled:
            return
        start_text_profile_sync_loop(
            repository=DataRepository(settings.database_url),
            llm_factory=lambda: get_llm_client(get_settings()),
            docs=get_docs_client(settings),
            interval_hours=settings.text_profile_sync_interval_hours,
            stop_event=stop,
        )

    def stop_workers() -> None:
        service.state.recovery_stop.set()
        service.state.recording_workers.close()
        service.state.recording_cleanup.close()

    @service.get("/health", response_model=HealthResponse)
    def health(
        repository: DataRepository = Depends(get_repository),
        settings: Settings = Depends(get_settings),
    ) -> HealthResponse:
        try:
            repository.healthcheck()
            details = repository.get_health_details()
        except Exception as exc:
            logger.exception("healthcheck falhou: database indisponivel")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Database unavailable: {exc}",
            ) from exc
        return HealthResponse(
            status="ok",
            database="ok",
            sessions_pending=details["sessions_pending"],
            sessions_failed=details["sessions_failed"],
            voice_profiles_pending=details["voice_profiles_pending"],
            voice_profiles_failed=details["voice_profiles_failed"],
            llm_provider=settings.llm_provider,
            llm_model=current_model(settings),
            recordings_transcribing=details["recordings_transcribing"],
            recordings_failed=details["recordings_failed"],
            recordings_completed=details["recordings_completed"],
            last_recording_status=details["last_recording_status"],
            last_recording_filename=details["last_recording_filename"],
            last_recording_at=details["last_recording_at"],
        )

    @service.get("/v1/speechmatics/keys", response_model=SpeechmaticsKeysResponse)
    def speechmatics_keys(
        settings: Settings = Depends(get_settings),
        repository: DataRepository = Depends(get_repository),
    ) -> SpeechmaticsKeysResponse:
        if settings.transcription_provider != "speechmatics":
            return SpeechmaticsKeysResponse(
                provider=settings.transcription_provider,
                limit_hours=settings.speechmatics_usage_limit_hours,
                selected_key=None,
                keys=[],
            )

        rows = get_speechmatics_key_usages(settings, repository)
        selected_key = None
        available_rows = [row for row in rows if row.available]
        if available_rows:
            selected_key = min(
                available_rows, key=speechmatics_key_usage_score
            ).key.name

        return SpeechmaticsKeysResponse(
            provider=settings.transcription_provider,
            limit_hours=settings.speechmatics_usage_limit_hours,
            selected_key=selected_key,
            keys=[speechmatics_key_usage_response(row) for row in rows],
            usage_note="Batch usage for the account/project accessible by each key. Today includes only completed recordings from this bot and is provisional. Percent is a configured hours budget, not credit balance. Keys may share usage.",
        )

    @service.post("/v1/transcriptions", response_model=TranscriptionAcceptedResponse)
    def create_transcription(
        recording_filename: str = Form(...),
        discord_id: str = Form(...),
        username: str = Form(...),
        channel_name: str = Form(...),
        recording_started_at: datetime = Form(...),
        session_id: int | None = Form(None),
        display_name: str | None = Form(None),
        settings: Settings = Depends(get_settings),
        repository: DataRepository = Depends(get_repository),
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
            receipt = repository.recording_receipt(
                recording_path.name, discord_id.strip(), session_id
            )
            if receipt is None or receipt["status"] not in {"completed", "discarded_no_credits"}:
                validate_recording_file(recording_path, settings)
            repository.start_recording(
                session_id=session_id,
                recording_filename=recording_path.name,
                discord_id=discord_id.strip(),
                metadata={
                    "discord_id": discord_id.strip(),
                    "username": username.strip(),
                    "display_name": display_name.strip() if display_name else None,
                    "channel_name": channel_name.strip(),
                    "recording_started_at": recording_started_at.isoformat(),
                },
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
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception(
                "API /v1/transcriptions erro recording_filename=%s discord_id=%s elapsed_ms=%d",
                recording_filename,
                discord_id,
                int((time.perf_counter() - started) * 1000),
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
            ) from exc

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

    @service.get("/v1/effort", response_model=LLMEffortResponse)
    def list_llm_efforts(
        settings: Settings = Depends(get_settings),
    ) -> LLMEffortResponse:
        if settings.llm_provider != "chatgpt":
            raise HTTPException(409, "Reasoning effort requires LLM_PROVIDER=chatgpt")
        return LLMEffortResponse(
            provider=settings.llm_provider,
            model=current_model(settings),
            current_effort=current_effort(settings),
            efforts=list(REASONING_EFFORTS),
        )

    @service.post("/v1/effort/current", response_model=SelectLLMEffortResponse)
    def change_llm_effort(
        request: SelectLLMEffortRequest,
        http_request: Request,
        settings: Settings = Depends(get_settings),
    ) -> SelectLLMEffortResponse:
        if settings.llm_provider != "chatgpt":
            raise HTTPException(409, "Reasoning effort requires LLM_PROVIDER=chatgpt")
        require_chatgpt_admin(http_request, settings)
        effort = request.effort.strip().lower()
        if effort not in REASONING_EFFORTS:
            raise HTTPException(400, "Invalid reasoning effort")
        model = current_model(settings)
        try:
            if not model:
                models = get_chatgpt_auth(settings).models()
                if not models:
                    raise HTTPException(
                        409, "No models available; sign in with make codex"
                    )
                # Let users fix an incompatible initial effort before /models
                # has saved a model. Test against the catalog without selecting it.
                model = models[0]["slug"]
            candidate = build_llm_client(settings, model, effort)
            test_response = candidate.test_model()
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(502, f"Effort test failed: {exc}") from exc
        select_effort(effort, settings.llm_model_selection_file)
        return SelectLLMEffortResponse(
            provider=settings.llm_provider,
            model=model,
            effort=effort,
            test_response=test_response,
        )

    @service.get("/v1/models", response_model=LLMModelsResponse)
    def list_llm_models(
        settings: Settings = Depends(get_settings),
    ) -> LLMModelsResponse:
        try:
            models = get_llm_client(settings).list_models()
        except Exception as exc:
            logger.exception(
                "falha ao listar modelos LLM provider=%s", settings.llm_provider
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Could not list LLM models: {exc}",
            ) from exc
        return LLMModelsResponse(
            provider=settings.llm_provider,
            current_model=current_model(settings),
            models=models,
        )

    @service.post("/v1/models/current", response_model=SelectLLMModelResponse)
    def change_llm_model(
        request: SelectLLMModelRequest,
        http_request: Request,
        settings: Settings = Depends(get_settings),
    ) -> SelectLLMModelResponse:
        if settings.llm_provider == "chatgpt":
            require_chatgpt_admin(http_request, settings)
        model = request.model.strip()
        if not model:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Model is required"
            )

        # ChatGPT's catalog can omit an ID; inference decides whether the
        # signed-in account can use an explicitly requested model.
        if (
            settings.llm_provider != "chatgpt"
            and model not in get_llm_client(settings).list_models()
        ):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Model is not available"
            )

        candidate = build_llm_client(settings, model, current_effort(settings))
        try:
            test_response = candidate.test_model()
        except Exception as exc:
            logger.exception(
                "teste do modelo LLM falhou provider=%s model=%s",
                settings.llm_provider,
                model,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Model test failed: {exc}",
            ) from exc

        select_model(settings.llm_provider, model, settings.llm_model_selection_file)
        logger.info(
            "modelo LLM alterado provider=%s model=%s", settings.llm_provider, model
        )
        return SelectLLMModelResponse(
            provider=settings.llm_provider,
            model=model,
            timeout_seconds=settings.llm_timeout_seconds,
            context_chars=settings.llm_context_chars,
            max_output_tokens=settings.llm_max_output_tokens,
            test_response=test_response,
        )

    @service.post("/v1/sessions", response_model=VoiceSessionResponse)
    def create_session(
        request: CreateSessionRequest,
        repository: DataRepository = Depends(get_repository),
        settings: Settings = Depends(get_settings),
    ) -> VoiceSessionResponse:
        validate_metadata(
            request.guild_id, request.voice_channel_id, request.channel_name
        )
        session = repository.create_voice_session(
            guild_id=request.guild_id,
            voice_channel_id=request.voice_channel_id,
            channel_name=request.channel_name,
            summary_channel_id=request.summary_channel_id,
            started_at=request.started_at or datetime.now(timezone.utc),
        )
        key_health.reset(configured_speechmatics_api_keys(settings))
        return voice_session_response(session)

    @service.post(
        "/v1/sessions/{session_id}/finish", response_model=VoiceSessionResponse
    )
    def finish_session(
        session_id: int,
        request: FinishSessionRequest,
        repository: DataRepository = Depends(get_repository),
        llm: LLMClient = Depends(get_llm_client),
        docs: LocalMarkdownProfileClient = Depends(get_docs_client),
    ) -> VoiceSessionResponse:
        session = repository.finish_voice_session(
            session_id,
            request.ended_at or datetime.now(timezone.utc),
            normalize_response_language(request.language),
        )
        if session is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Session not found"
            )
        maybe_schedule_session_agent(
            session_id, repository, llm, docs, language=request.language or "pt"
        )
        return voice_session_response(session)

    @service.get(
        "/v1/sessions/{session_id}/summary", response_model=SessionSummaryResponse
    )
    def get_session_summary(
        session_id: int,
        repository: DataRepository = Depends(get_repository),
    ) -> SessionSummaryResponse:
        session = repository.get_voice_session(session_id)
        if session is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Session not found"
            )
        return SessionSummaryResponse(
            session_id=session.id,
            status=session.status,
            summary=session.summary,
            agent_error=session.agent_error,
        )

    @service.get("/v1/sessions/{session_id}/voice-messages")
    def get_session_voice_messages(
        session_id: int,
        repository: DataRepository = Depends(get_repository),
    ) -> list[dict]:
        if repository.get_voice_session(session_id) is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return repository.get_session_messages(session_id)

    @service.delete("/v1/users/{discord_id}", response_model=ForgetUserResponse)
    def forget_user(
        discord_id: str,
        repository: DataRepository = Depends(get_repository),
        docs: LocalMarkdownProfileClient = Depends(get_docs_client),
        settings: Settings = Depends(get_settings),
    ) -> ForgetUserResponse:
        try:
            repository.invalidate_user_recordings(discord_id)
            deadline = time.monotonic() + 5
            while True:
                try:
                    result = repository.delete_user_by_discord_id(
                        discord_id,
                        remove_recording=lambda filename: remove_recording_files(
                            settings.recordings_dir, filename, include_request=True
                        ),
                    )
                    break
                except ValueError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.1)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if result is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
            )
        lore_deleted = docs.delete_doc(result.get("google_doc_id"))
        return ForgetUserResponse(
            status="deleted",
            discord_id=discord_id.strip(),
            messages_deleted=result["messages_deleted"],
            lore_file_deleted=lore_deleted,
        )

    @service.get("/v1/users/{discord_id}/profile", response_model=UserProfileResponse)
    def get_user_profile(
        discord_id: str,
        repository: DataRepository = Depends(get_repository),
    ) -> UserProfileResponse:
        profile = repository.get_user_profile_by_discord_id(discord_id)
        if profile is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
            )
        return user_profile_response(profile)

    @service.post("/v1/users/{discord_id}/prompt", response_model=ProfilePromptResponse)
    def prompt_user_profile(
        discord_id: str,
        request: ProfilePromptRequest,
        repository: DataRepository = Depends(get_repository),
        llm: LLMClient = Depends(get_llm_client),
        docs: LocalMarkdownProfileClient = Depends(get_docs_client),
    ) -> ProfilePromptResponse:
        question = " ".join(request.question.split())
        if not question:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Question is required"
            )

        profile = repository.get_user_profile_by_discord_id(discord_id)
        if profile is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
            )

        profile_doc_text = docs.read_doc_text(profile.google_doc_id)
        if not profile_doc_text.strip():
            profile_doc_text = (
                f"Current profile for {display_profile_name(profile)}:\n{profile.summary}\n"
                f"Interests: {profile.interests}\nCommunication: {profile.communication_style}\n"
                f"Observed patterns: {profile.known_facts}\nRecent updates: {profile.recent_updates}\n"
                f"Updated at: {profile.last_updated_at}"
            )
            if not (profile.summary or profile.interests or profile.known_facts):
                raise HTTPException(
                    status_code=404, detail="Profile has no observations yet"
                )

        username = display_profile_name(profile)
        answer = llm.answer_profile_question(
            username=username,
            profile_doc_text=profile_doc_text,
            question=question,
            language=request.language or "pt",
        )
        return ProfilePromptResponse(
            discord_id=profile.discord_id,
            username=profile.username,
            display_name=profile.display_name,
            anthropologist_title=profile.anthropologist_title,
            question=question,
            answer=answer,
        )

    @service.get("/v1/guilds/{guild_id}/recap", response_model=SessionRecapResponse)
    def get_guild_recap(
        guild_id: str,
        session_id: int | None = None,
        repository: DataRepository = Depends(get_repository),
    ) -> SessionRecapResponse:
        session = resolve_recap_session(repository, guild_id, session_id)
        recap_source, recap = build_session_recap(repository, session)
        return session_recap_response(session, recap_source, recap)

    @service.get("/v1/guilds/{guild_id}/guess", response_model=GuessResponse)
    def get_guild_guess(
        guild_id: str,
        repository: DataRepository = Depends(get_repository),
    ) -> GuessResponse:
        guess = repository.get_random_guess_message(guild_id)
        if guess is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No suitable voice quotes found for this guild",
            )
        return GuessResponse(
            quote=guess["quote"],
            options=guess["options"],
            correct_discord_id=guess["correct_discord_id"],
            correct_display_name=guess["correct_display_name"],
            session_id=guess["session_id"],
            channel_name=guess["channel_name"],
        )

    @service.post("/v1/guilds/{guild_id}/oracle", response_model=GuildOracleResponse)
    def ask_guild_oracle(
        guild_id: str,
        request: GuildOracleRequest,
        repository: DataRepository = Depends(get_repository),
        llm: LLMClient = Depends(get_llm_client),
    ) -> GuildOracleResponse:
        question = " ".join(request.question.split())
        if not question:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Question is required"
            )

        guild_context = repository.get_guild_oracle_context(guild_id, question)
        if not guild_context.strip():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No guild context available yet",
            )

        answer = llm.answer_guild_question(
            guild_context=guild_context,
            question=question,
            language=request.language or "pt",
        )
        return GuildOracleResponse(
            guild_id=guild_id.strip(), question=question, answer=answer
        )

    @service.post("/v1/guilds/{guild_id}/digest", response_model=TextDigestResponse)
    def text_digest(
        guild_id: str,
        request: TextDigestRequest,
        repository: DataRepository = Depends(get_repository),
        llm: LLMClient = Depends(get_llm_client),
    ):
        transcript, count, limited = repository.get_text_digest(
            guild_id, hours=request.hours, channel_id=request.channel_id
        )
        if not transcript.strip():
            raise HTTPException(
                status_code=404, detail="No stored text messages in this period"
            )
        summary = llm.summarize_session(
            transcript,
            session_context=f"Text conversations in the last {request.hours} hours. {count} stored messages. All dates are UTC.",
            language=request.language or "pt",
        )
        return TextDigestResponse(
            summary=summary, message_count=count, limited=limited, hours=request.hours
        )

    @service.post(
        "/v1/guilds/{guild_id}/sessions/{session_id}/retry",
        response_model=SessionSummaryResponse,
    )
    def retry_session(
        guild_id: str,
        session_id: int,
        repository: DataRepository = Depends(get_repository),
    ):
        session = resolve_recap_session(repository, guild_id, session_id)
        if session.status in {"open", "finished"}:
            raise HTTPException(
                status_code=409, detail="Session is still open or queued"
            )
        with repository.job_lock(102, session.id) as locked:
            if not locked:
                raise HTTPException(
                    status_code=409, detail="Session is being processed"
                )
            repository.retry_session(session.id)
        return SessionSummaryResponse(
            session_id=session.id, status="finished", summary=None, agent_error=None
        )

    install_streaming_routes(service, get_settings=get_settings, get_repository=get_repository,
        configured_keys=configured_speechmatics_api_keys,
        validate_filename=validate_recording_filename, resolve_path=resolve_recording_path)
    return service


@lru_cache
def get_settings() -> Settings:
    return Settings.from_env()


def get_repository(settings: Settings = Depends(get_settings)) -> DataRepository:
    return DataRepository(settings.database_url)


@lru_cache
def get_transcriber(settings: Settings = Depends(get_settings)) -> Transcriber:
    if settings.transcription_provider == "whisper":
        return WhisperTranscriber(
            settings.whisper_model,
            settings.whisper_device,
            language=settings.whisper_language,
            beam_size=settings.whisper_beam_size,
            fp16=settings.whisper_fp16,
            initial_prompt=settings.whisper_initial_prompt,
            carry_initial_prompt=settings.whisper_carry_initial_prompt,
            condition_on_previous_text=settings.whisper_condition_on_previous_text,
            hallucination_silence_threshold=settings.whisper_hallucination_silence_threshold,
            max_no_speech_prob=settings.whisper_max_no_speech_prob,
            no_speech_threshold=settings.whisper_no_speech_threshold,
            logprob_threshold=settings.whisper_logprob_threshold,
            compression_ratio_threshold=settings.whisper_compression_ratio_threshold,
            num_threads=settings.whisper_num_threads,
            vad_enabled=settings.whisper_vad_enabled,
            vad_aggressiveness=settings.whisper_vad_aggressiveness,
            vad_frame_ms=settings.whisper_vad_frame_ms,
            vad_padding_ms=settings.whisper_vad_padding_ms,
            vad_min_speech_ms=settings.whisper_vad_min_speech_ms,
        )
    if settings.transcription_provider == "speechmatics":
        return SpeechmaticsTranscriber(
            settings.speechmatics_api_key or "",
            api_keys=configured_speechmatics_api_keys(settings),
            batch_url=settings.speechmatics_batch_url,
            language=settings.speechmatics_language,
            model=settings.speechmatics_model,
            usage_limit_hours=settings.speechmatics_usage_limit_hours,
            usage_since=settings.speechmatics_usage_since,
            polling_interval_seconds=settings.speechmatics_polling_interval_seconds,
            timeout_seconds=settings.speechmatics_timeout_seconds,
            segment_gap_seconds=settings.speechmatics_segment_gap_seconds,
            additional_vocab=settings.speechmatics_additional_vocab,
        )
    raise RuntimeError(
        f"Unsupported TRANSCRIPTION_PROVIDER: {settings.transcription_provider}. "
        "Use 'whisper' or 'speechmatics'."
    )


def configured_speechmatics_api_keys(
    settings: Settings,
) -> tuple[SpeechmaticsAPIKey, ...]:
    keys = [
        SpeechmaticsAPIKey(name=name, value=value.strip())
        for name, value in settings.speechmatics_api_keys
        if value.strip()
    ]
    if not keys and settings.speechmatics_api_key:
        keys.append(
            SpeechmaticsAPIKey(
                name="SPEECHMATICS_API_KEY", value=settings.speechmatics_api_key.strip()
            )
        )
    return tuple({key.value: key for key in reversed(keys)}.values())[::-1]


def get_speechmatics_key_usages(
    settings: Settings, repository: DataRepository | None = None
) -> list[SpeechmaticsKeyUsage]:
    api_keys = configured_speechmatics_api_keys(settings)
    if not api_keys:
        return []
    rows = fetch_speechmatics_key_usages(
        api_keys=api_keys,
        batch_url=settings.speechmatics_batch_url,
        limit_hours=settings.speechmatics_usage_limit_hours,
        since=settings.speechmatics_usage_since,
    )
    local = repository.get_local_speechmatics_hours() if repository else {}
    combined = []
    for row in rows:
        if row.usage:
            today_hours = local.get(row.key.name, 0)
            used_hours = row.usage.used_hours + today_hours
            usage = replace(
                row.usage,
                used_hours=used_hours,
                local_today_hours=today_hours,
                percent_used=used_hours / row.usage.limit_hours * 100
                if row.usage.limit_hours > 0
                else None,
            )
            row = replace(row, usage=usage)
        combined.append(row)
    return combined


def speechmatics_key_usage_response(
    row: SpeechmaticsKeyUsage,
) -> SpeechmaticsKeyUsageResponse:
    if row.usage is None:
        return SpeechmaticsKeyUsageResponse(
            name=row.key.name,
            used_hours=None,
            limit_hours=0,
            percent_used=None,
            job_count=None,
            since=None,
            until=None,
            error=row.error or "usage unavailable",
        )
    return SpeechmaticsKeyUsageResponse(
        name=row.key.name,
        used_hours=row.usage.used_hours,
        limit_hours=row.usage.limit_hours,
        percent_used=row.usage.percent_used,
        job_count=row.usage.job_count,
        since=row.usage.since or None,
        until=row.usage.until or None,
        error=row.error,
        reported_hours=row.usage.reported_hours,
        local_today_hours=row.usage.local_today_hours,
    )


def get_llm_client(settings: Settings = Depends(get_settings)) -> LLMClient:
    return build_llm_client(settings, current_model(settings), current_effort(settings))


@lru_cache(maxsize=8)
def build_llm_client(
    settings: Settings, model: str, effort: str | None = None
) -> LLMClient:
    if settings.llm_provider == "chatgpt":
        return ChatGPTClient(
            auth=get_chatgpt_auth(settings),
            model=model,
            reasoning_effort=effort if effort is not None else current_effort(settings),
            timeout_seconds=settings.llm_timeout_seconds,
            context_chars=settings.llm_context_chars,
            max_output_tokens=settings.llm_max_output_tokens,
        )
    if settings.llm_provider == "ollama":
        return OllamaClient(
            base_url=settings.ollama_base_url,
            model=model,
            timeout_seconds=settings.llm_timeout_seconds,
            context_chars=settings.llm_context_chars,
            max_output_tokens=settings.llm_max_output_tokens,
        )
    if settings.llm_provider == "openai":
        return OpenAICompatibleClient(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            model=model,
            timeout_seconds=settings.llm_timeout_seconds,
            context_chars=settings.llm_context_chars,
            max_output_tokens=settings.llm_max_output_tokens,
        )
    if settings.llm_provider == "groq":
        return OpenAICompatibleClient(
            api_key=settings.groq_api_key,
            base_url=settings.groq_base_url,
            model=model,
            api_key_env="GROQ_API_KEY",
            provider_name="groq",
        )
    raise RuntimeError(
        f"Unsupported LLM_PROVIDER: {settings.llm_provider}. Use 'openai', 'groq', 'ollama', or 'chatgpt'."
    )


def get_docs_client(settings: Settings = Depends(get_settings)):
    if settings.profile_docs_provider != "local":
        raise RuntimeError(
            f"Unsupported PROFILE_DOCS_PROVIDER: {settings.profile_docs_provider}"
        )
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

    if (
        "/" in value
        or "\\" in value
        or value in {".", ".."}
        or Path(value).name != value
    ):
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
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Recording file is empty"
        )


def process_recording_file(
    *,
    recording_path: Path,
    session_id: int | None = None,
    recording_id: int | None = None,
    recording_lock_held: bool = False,
    provider_job_id: str | None = None,
    provider_key_name: str | None = None,
    discord_id: str,
    username: str,
    display_name: str | None,
    channel_name: str,
    recording_started_at: datetime,
    settings: Settings,
    repository: DataRepository,
    transcriber: Transcriber,
    llm: LLMClient | None = None,
    docs: LocalMarkdownProfileClient | None = None,
) -> None:
    started = time.perf_counter()
    try:
        logger.info(
            "job transcricao inicio file=%s bytes=%d provider=%s model=%s discord_id=%s",
            recording_path,
            recording_path.stat().st_size,
            transcriber.provider_name,
            transcriber.model_name,
            discord_id,
        )
        if (
            isinstance(transcriber, SpeechmaticsTranscriber)
            and recording_id is not None
        ):
            sidecar = recording_path.with_suffix(
                recording_path.suffix + ".speechmatics.json"
            )
            if not provider_job_id and sidecar.exists():
                remote = json.loads(sidecar.read_text(encoding="utf-8"))
                provider_job_id, provider_key_name = (
                    remote["job_id"],
                    remote["key_name"],
                )

            def save_job(job_id: str, key: str) -> None:
                repository.save_provider_job(recording_id, job_id, key)
                temporary = sidecar.with_suffix(".tmp")
                temporary.write_text(
                    json.dumps({"job_id": job_id, "key_name": key}), encoding="utf-8"
                )
                temporary.replace(sidecar)

            if provider_job_id:
                repository.save_provider_job(
                    recording_id, provider_job_id, provider_key_name
                )
            transcription_result = transcriber.transcribe_recording(
                recording_path,
                job_id=provider_job_id,
                key_name=provider_key_name,
                save_job=save_job,
                is_active=lambda: repository.recording_is_live(recording_id),
            )
        else:
            transcription_result = transcriber.transcribe(recording_path)
        duration_seconds = getattr(transcription_result, "duration_seconds", None)
        if recording_path.suffix.lower() == ".wav":
            with wave.open(str(recording_path), "rb") as audio:
                duration_seconds = audio.getnframes() / audio.getframerate()
        logger.info(
            "job transcricao concluido file=%s provider=%s discord_id=%s segmentos=%d texto_chars=%d",
            recording_path,
            transcriber.provider_name,
            discord_id,
            len(transcription_result.segments),
            len(transcription_result.text),
        )

        messages = messages_from_segments(recording_started_at, transcription_result)
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
            recording_id=recording_id,
            duration_seconds=duration_seconds,
            provider_completed_at=getattr(
                transcription_result, "provider_completed_at", None
            ),
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
        if recording_id is not None:
            RecordingCleanup(
                repository=repository, settings=settings
            ).cleanup_completed_file(
                recording_path.name,
                held_recording_id=recording_id if recording_lock_held else None,
            )
    except NoCredits:
        if session_id is not None:
            repository.discard_session_audio(session_id)
            RecordingCleanup(repository=repository, settings=settings).sweep()
        else:
            repository.mark_recording_failed(recording_id, "Speechmatics credits exhausted")
        logger.warning("Speechmatics exhausted session=%s; pending audio discarded", session_id)
    except Exception as exc:
        error_msg = (
            type(exc).__name__
            + ": transcription failed; retry retains the provider job ID"
        )
        try:
            repository.mark_recording_failed(recording_id, error_msg[:500])
        except Exception:
            logger.exception(
                "Could not persist recording failure; durable worker will recover it"
            )
        logger.warning(
            "job transcricao erro file=%s discord_id=%s elapsed_ms=%d",
            recording_path,
            discord_id,
            int((time.perf_counter() - started) * 1000),
        )
    finally:
        if session_id is not None and llm is not None and docs is not None:
            maybe_schedule_session_agent(session_id, repository, llm, docs)


def messages_from_segments(
    recording_started_at: datetime, result: TranscriptionResult
) -> list[MessageInsert]:
    started_at = normalize_timestamp(recording_started_at)
    return [
        MessageInsert(
            content=segment.text,
            tstamp=started_at + timedelta(seconds=segment.start),
        )
        for segment in result.segments
        if segment.text.strip()
    ]


def maybe_schedule_session_agent(
    session_id: int,
    repository: DataRepository,
    llm: LLMClient,
    docs: LocalMarkdownProfileClient,
    language: str = "pt",
) -> None:
    with _session_agent_lock:
        if session_id in _session_agent_pending or len(_session_agent_pending) >= 2:
            return
        _session_agent_pending.add(session_id)

    def runner() -> None:
        try:
            process_session_agent(
                session_id=session_id,
                repository=repository,
                llm=llm,
                docs=docs,
                language=language,
            )
        finally:
            with _session_agent_lock:
                _session_agent_pending.discard(session_id)

    threading.Thread(target=runner, daemon=True).start()


def process_session_agent(
    *,
    session_id: int,
    repository: DataRepository,
    llm: LLMClient,
    docs: LocalMarkdownProfileClient,
    language: str = "pt",
) -> None:
    with repository.job_lock(102, session_id) as locked:
        if not locked:
            return
        try:
            if not repository.claim_session_agent_run(session_id):
                return
            session = repository.get_voice_session(session_id)
            agent = SessionAgent(repository=repository, llm=llm, docs=docs)
            agent.run_for_session(
                session_id, language=session.response_language if session else language
            )
            logger.info("agent concluded session_id=%s", session_id)
        except Exception as exc:
            repository.mark_session_agent_failed(
                session_id, type(exc).__name__ + ": summary generation failed"
            )
            logger.exception("agent failed session_id=%s", session_id)


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
        anthropologist_title=profile.anthropologist_title,
        summary=profile.summary,
        interests=profile.interests,
        communication_style=profile.communication_style,
        persona_notes=profile.known_facts,
        recent_updates=profile.recent_updates,
        last_updated_at=profile.last_updated_at,
    )


def display_profile_name(profile: UserProfile) -> str:
    return profile.display_name or profile.username or profile.discord_id


def resolve_recap_session(
    repository: DataRepository,
    guild_id: str,
    session_id: int | None,
) -> VoiceSession:
    if session_id is not None:
        session = repository.get_voice_session(session_id)
        if session is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Session not found"
            )
        if session.guild_id != guild_id.strip():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Session not found"
            )
        return session

    session = repository.get_latest_voice_session(guild_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No voice sessions found"
        )
    return session


def build_session_recap(
    repository: DataRepository, session: VoiceSession
) -> tuple[str, str]:
    summary = (session.summary or "").strip()
    if summary:
        return "summary", summary

    if session.status in {"agent_running", "finished", "open", "agent_failed"}:
        messages = repository.get_session_messages(session.id)
        transcript_lines = [
            format_context_message_row(
                {
                    "tstamp": message["tstamp"],
                    "display_name": message.get("display_name"),
                    "username": message.get("username"),
                    "discord_id": message.get("discord_id"),
                    "channel_name": message.get("channel_name"),
                    "content": message.get("content"),
                    "source_type": "voice",
                },
                time_only=True,
            )
            for message in messages
        ]
        transcript_lines = [line for line in transcript_lines if line]
        if transcript_lines:
            transcript = "\n".join(transcript_lines)
            if session.status == "agent_failed":
                transcript = (
                    "⚠ A geração do resumo falhou; segue a conversa capturada. Usa /retry para recuperar.\n\n"
                    + transcript
                )
            return "transcript", transcript
        if session.status in {"agent_running", "open"}:
            return "pending", "A sessao ainda esta em curso ou a ser processada."

    if session.status == "agent_failed":
        return "error", (session.agent_error or "Session agent failed")
    return "pending", "Ainda nao ha resumo nem transcricao para esta sessao."


def session_recap_response(
    session: VoiceSession, recap_source: str, recap: str
) -> SessionRecapResponse:
    return SessionRecapResponse(
        session_id=session.id,
        guild_id=session.guild_id,
        channel_name=session.channel_name,
        started_at=session.started_at,
        ended_at=session.ended_at,
        status=session.status,
        recap_source=recap_source,
        recap=recap,
        agent_error=session.agent_error,
    )


app = create_app()
