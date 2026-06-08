from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timedelta

from data.repository import DataRepository, PendingTextProfile, UserProfile

from .agent import format_transcript
from .docs_client import LocalMarkdownProfileClient
from .llm import LLMClient


logger = logging.getLogger("uvicorn.error")

LOW_SIGNAL_MESSAGES = {
    "ok",
    "okay",
    "k",
    "kk",
    "lol",
    "lmao",
    "yes",
    "no",
    "sim",
    "nao",
    "não",
    "ya",
    "yep",
    "nope",
    "thanks",
    "obrigado",
    "obrigada",
}


def run_text_profile_sync(
    *,
    repository: DataRepository,
    llm: LLMClient,
    docs: LocalMarkdownProfileClient,
) -> int:
    updated = 0
    for pending in repository.get_pending_text_profiles():
        try:
            if update_text_profile(repository=repository, llm=llm, docs=docs, pending=pending):
                updated += 1
        except Exception:
            logger.exception("atualizacao de perfil por texto falhou user_id=%s", pending.user_id)
    return updated


def update_text_profile(
    *,
    repository: DataRepository,
    llm: LLMClient,
    docs: LocalMarkdownProfileClient,
    pending: PendingTextProfile,
) -> bool:
    messages = repository.get_text_messages_for_profile(pending.user_id, pending.last_text_seen_at)
    messages = [message for message in messages if is_profile_signal(str(message.get("content", "")))]
    if not messages:
        repository.mark_user_text_profile_seen(pending.user_id, pending.latest_message_at)
        return False

    current_profile = repository.get_user_profile_by_user_id(pending.user_id)
    username = display_profile_name(current_profile, pending)
    existing_doc_text = docs.read_doc_text(current_profile.google_doc_id if current_profile else None)
    observations = (
        f"Observation context: Text observations from {format_channel_list(messages)}\n\n"
        f"{format_transcript(messages)}"
    )
    generated = llm.update_profile_from_text(
        username=username,
        existing_profile=current_profile,
        existing_doc_text=existing_doc_text,
        observations=observations,
    )
    stored_doc = docs.upsert_profile_doc(
        doc_id=current_profile.google_doc_id if current_profile else None,
        username=username,
        profile=generated,
    )
    repository.upsert_user_profile(
        user_id=pending.user_id,
        anthropologist_title=generated.anthropologist_title,
        summary=generated.summary,
        interests=generated.interests,
        communication_style=generated.communication_style,
        known_facts=generated.persona_notes,
        recent_updates=generated.recent_updates,
        google_doc_id=stored_doc.doc_id,
        google_doc_url=stored_doc.url,
    )
    repository.mark_user_text_profile_seen(pending.user_id, pending.latest_message_at)
    return True


def is_profile_signal(content: str) -> bool:
    cleaned = " ".join(content.split()).strip()
    if len(cleaned) < 3:
        return False
    lowered = cleaned.lower()
    if lowered.startswith(("/", "!", ".")):
        return False
    if lowered in LOW_SIGNAL_MESSAGES:
        return False
    if re.fullmatch(r"[\W_]+", cleaned):
        return False
    return True


def display_profile_name(profile: UserProfile | None, pending: PendingTextProfile) -> str:
    if profile is not None:
        return profile.display_name or profile.username or profile.discord_id
    return pending.display_name or pending.username or pending.discord_id


def format_channel_list(messages: list[dict]) -> str:
    channels = []
    for message in messages:
        channel = str(message.get("channel_name", "")).strip()
        if channel and channel not in channels:
            channels.append(channel)
    if not channels:
        return "text chat"
    if len(channels) == 1:
        return channels[0]
    return ", ".join(channels[:3]) + (" and others" if len(channels) > 3 else "")


def next_midnight_aligned_run(now: datetime, interval_hours: int = 12) -> datetime:
    interval_hours = max(1, interval_hours)
    anchor = now.replace(hour=0, minute=0, second=0, microsecond=0)
    interval = timedelta(hours=interval_hours)
    candidate = anchor
    while candidate <= now:
        candidate += interval
    return candidate


def start_text_profile_sync_loop(
    *,
    repository: DataRepository,
    llm: LLMClient,
    docs: LocalMarkdownProfileClient,
    interval_hours: int = 12,
) -> threading.Thread:
    thread = threading.Thread(
        target=text_profile_sync_loop,
        kwargs={
            "repository": repository,
            "llm": llm,
            "docs": docs,
            "interval_hours": interval_hours,
        },
        daemon=True,
    )
    thread.start()
    return thread


def text_profile_sync_loop(
    *,
    repository: DataRepository,
    llm: LLMClient,
    docs: LocalMarkdownProfileClient,
    interval_hours: int = 12,
) -> None:
    while True:
        next_run = next_midnight_aligned_run(datetime.now().astimezone(), interval_hours)
        sleep_seconds = max(0.0, (next_run - datetime.now().astimezone()).total_seconds())
        logger.info("proxima sincronizacao de perfis por texto em %s", next_run.isoformat())
        time.sleep(sleep_seconds)
        try:
            updated = run_text_profile_sync(repository=repository, llm=llm, docs=docs)
            logger.info("sincronizacao de perfis por texto concluida perfis_atualizados=%d", updated)
        except Exception:
            logger.exception("sincronizacao de perfis por texto falhou")
