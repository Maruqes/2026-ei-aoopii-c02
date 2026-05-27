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
    summary: str
    interests: str
    communication_style: str
    persona_notes: str
    recent_updates: str
    google_doc_url: str | None
    last_updated_at: datetime | None
