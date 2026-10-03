from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class TranscriptionAcceptedResponse(BaseModel):
    status: str
    recording_filename: str
    message: str


class TextMessageRequest(BaseModel):
    guild_id: str
    channel_id: str
    channel_name: str
    discord_message_id: str
    discord_id: str
    username: str
    display_name: str | None = None
    content: str
    tstamp: datetime
    edited_at: datetime | None = None


class TextMessageResponse(BaseModel):
    status: str
    user_id: int
    message_id: int


class TextProfileSyncResponse(BaseModel):
    status: str
    updated_profiles: int
    processing_ms: int


class LLMEffortResponse(BaseModel):
    provider: str
    model: str
    current_effort: str
    efforts: list[str]


class SelectLLMEffortRequest(BaseModel):
    effort: str


class SelectLLMEffortResponse(BaseModel):
    provider: str
    model: str
    effort: str
    test_response: str


class LLMModelsResponse(BaseModel):
    provider: str
    current_model: str
    models: list[str]


class SelectLLMModelRequest(BaseModel):
    model: str


class SelectLLMModelResponse(BaseModel):
    provider: str
    model: str
    test_response: str


class ProfilePromptRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    language: str | None = None


class ProfilePromptResponse(BaseModel):
    discord_id: str
    username: str
    display_name: str | None
    anthropologist_title: str
    question: str
    answer: str


class CreateSessionRequest(BaseModel):
    guild_id: str
    voice_channel_id: str
    channel_name: str
    summary_channel_id: str | None = None
    started_at: datetime | None = None


class VoiceSessionResponse(BaseModel):
    id: int
    guild_id: str
    voice_channel_id: str
    channel_name: str
    summary_channel_id: str | None
    started_at: datetime
    ended_at: datetime | None
    status: str
    summary: str | None
    agent_error: str | None


class FinishSessionRequest(BaseModel):
    ended_at: datetime | None = None
    language: str | None = None


class SessionSummaryResponse(BaseModel):
    session_id: int
    status: str
    summary: str | None
    agent_error: str | None


class UserProfileResponse(BaseModel):
    discord_id: str
    username: str
    display_name: str | None
    anthropologist_title: str
    summary: str
    interests: str
    communication_style: str
    persona_notes: str
    recent_updates: str
    last_updated_at: datetime | None


class HealthResponse(BaseModel):
    sessions_pending: int = 0
    sessions_failed: int = 0
    voice_profiles_pending: int = 0
    voice_profiles_failed: int = 0
    llm_provider: str = ""
    llm_model: str = ""
    status: str
    database: str
    recordings_transcribing: int
    recordings_failed: int
    recordings_completed: int
    last_recording_status: str | None
    last_recording_filename: str | None
    last_recording_at: datetime | None


class SpeechmaticsKeyUsageResponse(BaseModel):
    reported_hours: float | None = None
    local_today_hours: float = 0
    name: str
    used_hours: float | None
    limit_hours: float
    percent_used: float | None
    job_count: int | None
    since: str | None
    until: str | None
    error: str | None


class SpeechmaticsKeysResponse(BaseModel):
    usage_note: str = ""
    provider: str
    limit_hours: float
    selected_key: str | None
    keys: list[SpeechmaticsKeyUsageResponse]


class ForgetUserResponse(BaseModel):
    status: str
    discord_id: str
    messages_deleted: int
    lore_file_deleted: bool


class GuildOracleRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    language: str | None = None


class GuildOracleResponse(BaseModel):
    guild_id: str
    question: str
    answer: str


class GuessResponse(BaseModel):
    quote: str
    options: list[str]
    correct_discord_id: str
    correct_display_name: str
    session_id: int | None
    channel_name: str | None


class SessionRecapResponse(BaseModel):
    session_id: int
    guild_id: str
    channel_name: str
    started_at: datetime
    ended_at: datetime | None
    status: str
    recap_source: str
    recap: str
    agent_error: str | None


class TextDigestRequest(BaseModel):
    hours: int = Field(default=24, ge=1, le=168)
    channel_id: str | None = None
    language: str | None = None


class TextDigestResponse(BaseModel):
    summary: str
    message_count: int
    limited: bool
    hours: int
