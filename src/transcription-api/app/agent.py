from __future__ import annotations

import logging
from datetime import datetime

from data.repository import DataRepository, SessionParticipant, UserProfile, normalize_timestamp

logger = logging.getLogger("uvicorn.error")

from .docs_client import LocalMarkdownProfileClient
from .llm import GeneratedProfile, LLMClient, LoreEvent


class SessionAgent:
    def __init__(
        self,
        *,
        repository: DataRepository,
        llm: LLMClient,
        docs: LocalMarkdownProfileClient,
    ):
        self.repository = repository
        self.llm = llm
        self.docs = docs

    def run_for_session(self, session_id: int) -> str:
        session = self.repository.get_voice_session(session_id)
        observation_context = f"Voice session in {session.channel_name}" if session else "Voice session"
        messages = self.repository.get_session_messages(session_id)
        transcript = format_transcript(messages)
        if not transcript:
            summary = "No voice transcript was captured for this session."
            self.repository.mark_session_agent_done(session_id, summary)
            return summary

        summary = self.llm.summarize_session(transcript, session_context=format_session_context(session))
        participants = self.repository.get_session_participants(session_id)
        for participant in participants:
            try:
                current_profile = self.repository.get_user_profile_by_discord_id(participant.discord_id)
                participant_transcript = format_transcript(
                    [message for message in messages if message["discord_id"] == participant.discord_id]
                )
                existing_doc_text = self.docs.read_doc_text(current_profile.google_doc_id if current_profile else None)
                generated = self.llm.update_profile(
                    username=display_name(participant),
                    existing_profile=current_profile,
                    existing_doc_text=existing_doc_text,
                    transcript=(
                        f"Observation context: {observation_context}\n\n"
                        f"Full session:\n{transcript}\n\n"
                        f"Messages by {display_name(participant)}:\n{participant_transcript}"
                    ),
                )
                stored_doc = self.docs.upsert_profile_doc(
                    doc_id=current_profile.google_doc_id if current_profile else None,
                    username=display_name(participant),
                    profile=generated,
                )
                self.repository.upsert_user_profile(
                    user_id=participant.user_id,
                    anthropologist_title=generated.anthropologist_title,
                    summary=generated.summary,
                    interests=generated.interests,
                    communication_style=generated.communication_style,
                    known_facts=generated.persona_notes,
                    recent_updates=generated.recent_updates,
                    google_doc_id=stored_doc.doc_id,
                    google_doc_url=stored_doc.url,
                )
            except Exception:
                logger.exception(
                    "falha ao atualizar perfil do participante session_id=%s discord_id=%s",
                    session_id,
                    participant.discord_id,
                )

        self.repository.mark_session_agent_done(session_id, summary)
        return summary


def display_name(participant: SessionParticipant | UserProfile) -> str:
    return participant.display_name or participant.username or participant.discord_id


def format_transcript(messages: list[dict]) -> str:
    lines: list[str] = []
    for message in messages:
        tstamp = normalize_timestamp_value(message["tstamp"])
        username = message.get("display_name") or message.get("username") or message.get("discord_id")
        content = " ".join(str(message.get("content", "")).split())
        if content:
            lines.append(f"[{tstamp:%H:%M}] {username}: {content}")
    return "\n".join(lines)


def format_session_context(session) -> str:
    if session is None:
        return "Voice session"
    started = normalize_timestamp_value(session.started_at)
    parts = [
        f"Channel: {session.channel_name}",
        f"Started at: {started:%Y-%m-%d %H:%M UTC}",
    ]
    if session.ended_at is not None:
        ended = normalize_timestamp_value(session.ended_at)
        parts.append(f"Ended at: {ended:%Y-%m-%d %H:%M UTC}")
    return "\n".join(parts)


def normalize_timestamp_value(value) -> datetime:
    if isinstance(value, datetime):
        return normalize_timestamp(value)
    return normalize_timestamp(datetime.fromisoformat(str(value)))


def blank_profile() -> GeneratedProfile:
    return GeneratedProfile(
        anthropologist_title="",
        summary="",
        interests="",
        communication_style="",
        persona_notes="",
        recent_updates="",
        lore_event=LoreEvent(
            title="",
            new_observations=[],
            reinforced_patterns=[],
            changed_interpretations=[],
            weakened_or_retired_patterns=[],
        ),
    )
