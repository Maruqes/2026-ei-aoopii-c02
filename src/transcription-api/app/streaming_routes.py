from __future__ import annotations

import asyncio

from fastapi import Depends, HTTPException, WebSocket
from pydantic import BaseModel, Field

from .provider_routes import install_provider_routes
from .providers import effective_order, registry_for
from .realtime import RealtimePool, bridge, valid_configuration


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
    speechmatics_keys=None,
):
    def pool_for(settings):
        pool = getattr(service.state, "realtime_pool", None)
        if pool is None:
            pool = RealtimePool((), registry=registry_for(settings))
            service.state.realtime_pool = pool
        return pool

    async def status(guild_id, settings, repository):
        pool = pool_for(settings)
        preferred = await asyncio.to_thread(
            repository.streaming_preference,
            guild_id,
            settings.transcription_streaming_enabled,
        )
        order, source = await asyncio.to_thread(effective_order, settings, repository, guild_id)
        eligible = tuple(k for k in pool.keys if k.provider in order and pool.registry.supports(k.provider, "streaming"))
        usable = valid_configuration(settings, eligible, order) and any(
            pool.registry.state(k, "streaming") not in {"no_credits", "invalid_key"} for k in eligible)
        groups = {(k.provider, pool.registry.group(k)): k for k in eligible}
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
        capacity = sum(pool.registry.limits(k, "streaming") for k in groups.values())
        dg_capacity = sum(pool.registry.limits(k, "streaming") for k in groups.values() if k.provider == "deepgram")
        if any(k.provider == "deepgram" and k.name not in pool.registry.verified for k in eligible):
            capacity -= max(0, dg_capacity - settings.deepgram_streaming_limit)
        return {
            "enabled": preferred and usable,
            "preferred": preferred,
            "available": usable,
            "occupied": occupied,
            "capacity": capacity,
            "order": order, "source": source,
            "providers": {p: sum(not r.retiring and r.key.provider == p for r in pool.reservations.values())
                          for p in ("deepgram", "speechmatics")},
            "capture_suspended": order != ("whisper",) and not pool.registry.usable(order),
            "queued": getattr(service.state, "realtime_queue_sizes", {}).get(
                guild_id, 0
            ),
        }

    if speechmatics_keys is not None:
        install_provider_routes(service, get_settings=get_settings, get_repository=get_repository,
                                speechmatics_keys=speechmatics_keys)

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
            order, _ = await asyncio.to_thread(effective_order, settings, repository, guild_id)
            if not valid_configuration(settings, pool.keys, order):
                raise HTTPException(
                    409,
                    "Streaming requires a configured provider with a compatible streaming model/language",
                )
            pool.registry.reset()
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
            session_id, list(dict.fromkeys(request.users)), enabled, mode["order"]
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
