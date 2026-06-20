from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class TranscriptSegment(BaseModel):
    start: float
    end: float
    text: str


class ChunkResponse(BaseModel):
    id: int
    channel_name: str
    start_at: datetime
    end_at: datetime


class TranscriptionResponse(BaseModel):
    text: str
    segments: list[TranscriptSegment]
    user_id: int
    message_ids: list[int]
    affected_chunks: list[ChunkResponse]
    model: str
    processing_ms: int


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
    question: str


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
    status: str
    database: str
    recordings_transcribing: int
    recordings_failed: int
    recordings_completed: int
    last_recording_status: str | None
    last_recording_filename: str | None
    last_recording_at: datetime | None


class SpeechmaticsKeyUsageResponse(BaseModel):
    name: str
    used_hours: float | None
    limit_hours: float
    percent_used: float | None
    job_count: int | None
    since: str | None
    until: str | None
    error: str | None


class SpeechmaticsKeysResponse(BaseModel):
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
    question: str


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
