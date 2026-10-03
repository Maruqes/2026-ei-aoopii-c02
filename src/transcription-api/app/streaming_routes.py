from __future__ import annotations

import asyncio

from fastapi import Depends, HTTPException, WebSocket
from pydantic import BaseModel, Field

from .realtime import RealtimePool, bridge, valid_configuration
from .speechmatics_errors import key_health


class StreamingMode(BaseModel):
    mode: str


class CreditNotice(BaseModel):
    episode: int


class StreamingRoster(BaseModel):
    users: list[str] = Field(default_factory=list, max_length=1000)


def install_streaming_routes(
    service,
    *,
    get_settings,
    get_repository,
    configured_keys,
    validate_filename,
    resolve_path,
):
    def pool_for(settings):
        pool = getattr(service.state, "realtime_pool", None)
        if pool is None:
            pool = RealtimePool(configured_keys(settings))
            service.state.realtime_pool = pool
        return pool

    async def status(guild_id, settings, repository):
        pool = pool_for(settings)
        preferred = await asyncio.to_thread(
            repository.streaming_preference,
            guild_id,
            settings.transcription_streaming_enabled,
        )
        usable = valid_configuration(settings, pool.keys) and any(
            key_health.healthy(k.value) for k in pool.keys
        )
        session = (
            await asyncio.to_thread(repository.get_voice_session, pool.session_id)
            if pool.session_id
            else None
        )
        if session and session.guild_id == guild_id:
            credit_state = await asyncio.to_thread(
                repository.session_credit_state, session.id
            )
            if credit_state["exhausted"]:
                usable = False
        occupied = (
            sum(not r.retiring for r in pool.reservations.values())
            if session and session.guild_id == guild_id
            else 0
        )
        return {
            "enabled": preferred and usable,
            "preferred": preferred,
            "available": usable,
            "occupied": occupied,
            "capacity": len(pool.keys) * 2,
            "queued": getattr(service.state, "realtime_queue_sizes", {}).get(
                guild_id, 0
            ),
        }

    @service.get("/v1/guilds/{guild_id}/streaming")
    async def get_mode(
        guild_id: str,
        settings=Depends(get_settings),
        repository=Depends(get_repository),
    ):
        return await status(guild_id, settings, repository)

    @service.post("/v1/guilds/{guild_id}/streaming")
    async def set_mode(
        guild_id: str,
        request: StreamingMode,
        settings=Depends(get_settings),
        repository=Depends(get_repository),
    ):
        if request.mode not in {"on", "off"}:
            raise HTTPException(422, "mode must be on or off")
        pool = pool_for(settings)
        enabled = request.mode == "on"
        if enabled:
            if not valid_configuration(settings, pool.keys):
                raise HTTPException(
                    409,
                    "Streaming requires Speechmatics keys, Realtime enhanced/standard and language pt",
                )
            key_health.reset(pool.keys)
            if pool.session_id:
                session = await asyncio.to_thread(
                    repository.get_voice_session, pool.session_id
                )
                if session and session.guild_id == guild_id:
                    await asyncio.to_thread(
                        repository.reset_session_credits, session.id
                    )
        await asyncio.to_thread(repository.set_streaming_preference, guild_id, enabled)
        return await status(guild_id, settings, repository)

    @service.post("/v1/sessions/{session_id}/streaming")
    async def roster(
        session_id: int,
        request: StreamingRoster,
        settings=Depends(get_settings),
        repository=Depends(get_repository),
    ):
        session = await asyncio.to_thread(repository.get_voice_session, session_id)
        if session is None:
            raise HTTPException(404, "Session not found")
        pool = pool_for(settings)
        state = await asyncio.to_thread(repository.session_credit_state, session_id)
        mode = await status(session.guild_id, settings, repository)
        enabled = (
            mode["enabled"] and session.status == "open" and not state["exhausted"]
        )
        assignments = pool.reconcile(
            session_id, list(dict.fromkeys(request.users)), enabled
        )
        queues = getattr(service.state, "realtime_queue_sizes", {})
        queues[session.guild_id] = (
            len(request.users) - len(assignments) if enabled else 0
        )
        service.state.realtime_queue_sizes = queues
        mode["enabled"] = enabled and pool.session_id == session_id
        mode["occupied"] = (
            sum(not r.retiring for r in pool.reservations.values())
            if pool.session_id == session_id
            else 0
        )
        return {
            **mode,
            "assignments": assignments,
            "queued": queues[session.guild_id],
            **state,
        }

    @service.post("/v1/sessions/{session_id}/credits/notice")
    async def notice(
        session_id: int, request: CreditNotice, repository=Depends(get_repository)
    ):
        await asyncio.to_thread(
            repository.acknowledge_credit_notice, session_id, request.episode
        )
        return {"status": "acknowledged"}

    @service.get("/v1/credits/notices")
    def notices(repository=Depends(get_repository)):
        return repository.pending_credit_notices()

    @service.get("/v1/speechmatics/realtime-usage")
    def realtime_usage(repository=Depends(get_repository)):
        return {"product": "realtime", "local_hours": repository.local_realtime_hours()}

    @service.websocket("/v1/streaming/audio")
    async def audio(
        websocket: WebSocket,
        settings=Depends(get_settings),
        repository=Depends(get_repository),
    ):
        await bridge(
            websocket,
            settings=settings,
            repository=repository,
            pool=pool_for(settings),
            validate_filename=validate_filename,
            resolve_path=resolve_path,
        )
