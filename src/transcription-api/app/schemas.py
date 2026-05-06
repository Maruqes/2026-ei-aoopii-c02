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
