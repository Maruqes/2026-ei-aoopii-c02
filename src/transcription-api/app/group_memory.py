from __future__ import annotations

import logging
import threading
import zlib
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import Depends, HTTPException
from pydantic import BaseModel

from data import group_memory as memory

from .agent import format_transcript
from .profile_updater import is_profile_signal, update_observed_profile

logger = logging.getLogger("uvicorn.error")


def run_group_memory_tick(*, repository, llm_factory, docs, settings, now=None):
    now = now or datetime.now(timezone.utc)
    minutes = settings.group_memory_bulk_minutes
    for guild in memory.memory_guilds(
        repository, now, minutes, settings.group_memory_context_bulks
    ):
        # Hash collisions only serialize unrelated guilds; their data remains separately scoped.
        with repository.job_lock(
            104, zlib.crc32(guild.encode()) & 0x7FFFFFFF
        ) as locked:
            if not locked:
                continue
            try:
                start, messages = memory.next_bulk_messages(
                    repository, guild, now, minutes
                )
                if messages:
                    llm = llm_factory()
                    previous = memory.recent_bulks(
                        repository, guild, settings.group_memory_context_bulks
                    )
                    previous = [
                        b
                        for b in previous
                        if b["end_at"]
                        > start
                        - timedelta(
                            minutes=minutes * (settings.group_memory_context_bulks - 1)
                        )
                    ]
                    assistant = repository.assistant_settings(guild)
                    cutoff = now - timedelta(
                        minutes=settings.group_memory_reaction_cooldown_minutes
                    )
                    allowed = (
                        settings.group_memory_reactions_enabled
                        and assistant["enabled"]
                        and start + timedelta(minutes=minutes)
                        >= now - timedelta(minutes=minutes)
                        and not any(
                            b["reaction_text"] and b["created_at"] > cutoff
                            for b in memory.recent_bulks(repository, guild, 100)
                        )
                    )
                    previous_text = "\n\n".join(
                        f"Bulk {b['start_at'].isoformat()} – {b['end_at'].isoformat()}:\n{b['summary']}\nLore: {b['lore']}\nBot reaction (generated): {b['reaction_text']}"
                        for b in reversed(previous)
                    )
                    generated = llm.analyze_group_bulk(
                        observations=f"Window {start.isoformat()} – {(start + timedelta(minutes=minutes)).isoformat()}\n"
                        + format_transcript(messages),
                        previous_bulks=previous_text,
                        reaction_allowed=allowed,
                    )
                    # Enforce cooldown even for an LLM provider returning a reaction despite the flag.
                    if not allowed:
                        generated.update(reaction_text="", gif_query="", speak=False)
                    destination = assistant["channel_id"] or next(
                        (
                            m["destination"]
                            for m in reversed(messages)
                            if m["destination"]
                        ),
                        None,
                    )
                    memory.save_bulk(
                        repository,
                        guild,
                        start,
                        start + timedelta(minutes=minutes),
                        messages,
                        generated,
                        destination,
                    )
                for bulk_id, user_id, own in memory.profile_jobs(repository, guild):
                    try:
                        with repository.job_lock(103, user_id, wait=True):
                            profile = repository.get_user_profile_by_user_id(user_id)
                            own = [
                                m
                                for m in own
                                if is_profile_signal(m["content"])
                                and (
                                    m["source_type"] == "voice"
                                    or not profile
                                    or not profile.last_text_seen_at
                                    or m["tstamp"] > profile.last_text_seen_at
                                )
                            ]
                            if own:
                                person = own[0]
                                update_observed_profile(
                                    repository=repository,
                                    llm=llm_factory(),
                                    docs=docs,
                                    user_id=user_id,
                                    discord_id=person["discord_id"],
                                    username=person["display_name"]
                                    or person["username"],
                                    observations=f"Observation context: Group bulk #{bulk_id}. Only this member's messages are evidence.\n"
                                    + format_transcript(own),
                                    observed_at=max(m["tstamp"] for m in own),
                                    observation_id=f"bulk-{bulk_id}-user-{user_id}-through-{max(m['id'] for m in own)}",
                                )
                                text_times = [
                                    m["tstamp"]
                                    for m in own
                                    if m["source_type"] in {"text", "assistant"}
                                ]
                                if text_times:
                                    repository.mark_user_text_profile_seen(
                                        user_id, max(text_times)
                                    )
                            memory.mark_profile_done(repository, bulk_id, user_id)
                    except Exception:
                        logger.exception(
                            "Bulk profile failed bulk=%s user=%s", bulk_id, user_id
                        )
            except Exception:
                logger.exception("Group memory tick failed guild=%s", guild)


def start_group_memory_loop(*, repository, llm_factory, docs, settings, stop_event):
    def run():
        while not stop_event.is_set():
            try:
                run_group_memory_tick(
                    repository=repository,
                    llm_factory=llm_factory,
                    docs=docs,
                    settings=settings,
                )
            except Exception:
                logger.exception("Group memory queue unavailable")
            if stop_event.wait(15):
                return

    thread = threading.Thread(target=run, name="group-memory", daemon=True)
    thread.start()
    return thread


class ReactionResult(BaseModel):
    status: Literal["sent", "failed"]


def install_group_memory_routes(service, *, get_repository, get_settings):
    @service.get("/v1/memory/reactions")
    def reactions(repository=Depends(get_repository), settings=Depends(get_settings)):
        if (
            not settings.group_memory_enabled
            or not settings.group_memory_reactions_enabled
        ):
            return []
        return memory.pending_reactions(
            repository, datetime.now(timezone.utc), settings.group_memory_bulk_minutes
        )

    @service.post("/v1/memory/reactions/{bulk_id}/claim")
    def claim(
        bulk_id: int, repository=Depends(get_repository), settings=Depends(get_settings)
    ):
        if (
            not settings.group_memory_enabled
            or not settings.group_memory_reactions_enabled
            or not memory.claim_reaction(
                repository,
                bulk_id,
                datetime.now(timezone.utc),
                settings.group_memory_bulk_minutes,
            )
        ):
            raise HTTPException(409, "Reaction expired, disabled or already claimed")
        return {"status": "claimed"}

    @service.post("/v1/memory/reactions/{bulk_id}/result")
    def result(
        bulk_id: int, request: ReactionResult, repository=Depends(get_repository)
    ):
        memory.finish_reaction(repository, bulk_id, request.status)
        return {"status": request.status}

    @service.get("/v1/guilds/{guild_id}/memory")
    def inspect(
        guild_id: str,
        repository=Depends(get_repository),
        settings=Depends(get_settings),
    ):
        return {
            "bulk_minutes": settings.group_memory_bulk_minutes,
            "context_bulks": settings.group_memory_context_bulks,
            "bulks": memory.recent_bulks(
                repository, guild_id, settings.group_memory_context_bulks
            ),
        }
