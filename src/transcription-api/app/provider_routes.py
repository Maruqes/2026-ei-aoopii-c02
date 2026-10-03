from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime, timezone
from urllib.parse import quote

import httpx
from fastapi import Depends, HTTPException
from pydantic import BaseModel

from .providers import REMOTE_PROVIDERS, effective_order, parse_order, registry_for


class ProviderOrder(BaseModel):
    providers: str


def deepgram_management(settings, registry):
    with registry.lock:
        cached = registry.management_cache.get("deepgram")
        if cached and time.monotonic() - cached[0] < 60:
            return cached[1]
    rows = {}
    deadline = time.monotonic() + 8
    today = datetime.now(timezone.utc).date()
    since = today.replace(day=1).isoformat()
    with httpx.Client(timeout=2) as client:
        for key in registry.for_provider("deepgram"):
            try:
                if key.name not in registry.verified and time.monotonic() < deadline:
                    response = client.get(
                        settings.deepgram_api_base_url + "/projects",
                        headers={"Authorization": "Token " + key.value},
                    )
                    response.raise_for_status()
                    projects = response.json().get("projects", [])
                    if len(projects) == 1:
                        registry.reconcile_project(key, projects[0]["project_id"])
            except Exception:
                pass  # Management permissions never affect inference health.
            project = registry.group(key)
            if project == "unverified":
                continue
            row = rows.get(project) or {
                "balance_usd": None,
                "balance_error": "unavailable",
                "reported_hours": None,
                "management_error": "unavailable",
                "since": since,
                "until": today.isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            if time.monotonic() < deadline:
                base = (
                    settings.deepgram_api_base_url
                    + "/projects/"
                    + quote(project, safe="")
                )
                for operation in ("balances", "usage/breakdown"):
                    field = (
                        "balance_usd" if operation == "balances" else "reported_hours"
                    )
                    if row[field] is not None:
                        continue
                    if time.monotonic() >= deadline:
                        break
                    try:
                        response = client.get(
                            base + "/" + operation,
                            headers={"Authorization": "Token " + key.value},
                            params={
                                "start": since,
                                "end": today.isoformat(),
                                "endpoint": "listen",
                            }
                            if operation != "balances"
                            else None,
                        )
                        response.raise_for_status()
                        data = response.json()
                        if operation == "balances":
                            amounts = [
                                float(b["amount"])
                                for b in data.get("balances", [])
                                if b.get("units") == "USD"
                            ]
                            if not all(math.isfinite(value) for value in amounts):
                                raise ValueError("Invalid balance")
                            row["balance_usd"] = sum(amounts) if amounts else None
                            row["balance_error"] = None if amounts else "unavailable"
                        else:
                            entries = data.get("results")
                            if isinstance(entries, list):
                                hours = [float(r["hours"]) for r in entries]
                                if not all(math.isfinite(h) and h >= 0 for h in hours):
                                    raise ValueError("Invalid usage")
                                row["reported_hours"] = sum(hours)
                        row["management_error"] = None
                    except httpx.HTTPStatusError as exc:
                        if operation == "balances":
                            row["balance_error"] = (
                                "forbidden"
                                if exc.response.status_code == 403
                                else "unavailable"
                            )
                        row["management_error"] = "unavailable"
                    except Exception:
                        if operation == "balances":
                            row["balance_error"] = "unavailable"
                        row["management_error"] = "unavailable"
            rows[project] = row
    with registry.lock:
        registry.management_cache["deepgram"] = (time.monotonic(), rows)
    return rows


def install_provider_routes(
    service, *, get_settings, get_repository, speechmatics_keys
):
    def status(guild_id, settings, repository):
        order, source = effective_order(settings, repository, guild_id)
        registry = registry_for(settings)
        with registry.lock:
            in_use = sorted({key.provider for key, _ in registry.leases.values()})
        return {
            "order": order,
            "source": source,
            "in_use": in_use,
            "configured": {p: bool(registry.for_provider(p)) for p in REMOTE_PROVIDERS},
            "capture_suspended": order != ("whisper",) and not registry.usable(order),
            "capture_reason": "credits_exhausted"
            if registry.exhausted(order)
            else "unconfigured_or_invalid_credentials",
            "streaming": repository.streaming_preference(
                guild_id, settings.transcription_streaming_enabled
            ),
        }

    @service.get("/v1/guilds/{guild_id}/transcription")
    def get_order(
        guild_id: str,
        settings=Depends(get_settings),
        repository=Depends(get_repository),
    ):
        return status(guild_id, settings, repository)

    @service.post("/v1/guilds/{guild_id}/transcription")
    def set_order(
        guild_id: str,
        request: ProviderOrder,
        settings=Depends(get_settings),
        repository=Depends(get_repository),
    ):
        try:
            order = parse_order(request.providers)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        registry = registry_for(settings)
        if not any(registry.for_provider(p) for p in order):
            raise HTTPException(409, "No configured provider in this order")
        repository.set_transcription_order(guild_id, order)
        # A newly permitted provider can resume a call suspended by the old order.
        if registry.usable(order):
            session = repository.get_latest_voice_session(guild_id)
            if session and session.status == "open":
                repository.reset_session_credits(session.id)
        return status(guild_id, settings, repository)

    @service.get("/v1/transcription/keys")
    async def keys(
        guild_id: str | None = None,
        settings=Depends(get_settings),
        repository=Depends(get_repository),
    ):
        registry = registry_for(settings)
        order, source = await asyncio.to_thread(
            effective_order, settings, repository, guild_id
        )
        since = datetime.now(timezone.utc).date().replace(day=1).isoformat()
        local = await asyncio.to_thread(repository.local_provider_usage, since)

        async def cached_speechmatics():
            with registry.lock:
                cached = registry.management_cache.get("speechmatics")
                if cached and time.monotonic() - cached[0] < 60:
                    return cached[1]
            try:
                response = await asyncio.wait_for(
                    asyncio.to_thread(speechmatics_keys, settings, repository), 12
                )
                result = response.model_dump()
            except Exception:
                result = {
                    "keys": [
                        {"name": k.name, "error": "management unavailable"}
                        for k in registry.for_provider("speechmatics")
                    ],
                    "usage_note": "unavailable",
                }
            with registry.lock:
                registry.management_cache["speechmatics"] = (time.monotonic(), result)
            return result

        outcomes = await asyncio.gather(
            asyncio.wait_for(
                asyncio.to_thread(deepgram_management, settings, registry), 12
            ),
            cached_speechmatics(),
            return_exceptions=True,
        )
        management = outcomes[0] if isinstance(outcomes[0], dict) else {}
        sm = (
            outcomes[1]
            if isinstance(outcomes[1], dict)
            else {"keys": [], "usage_note": "unavailable"}
        )
        providers = []
        with registry.lock:
            for provider in REMOTE_PROVIDERS:
                configured = registry.for_provider(provider)
                groups, key_rows = {}, []
                for key in configured:
                    group = registry.group(key)
                    key_rows.append(
                        {
                            "name": key.name,
                            "group": group,
                            "streaming_state": registry.state(key, "streaming"),
                            "batch_state": registry.state(key, "batch"),
                            "occupied": registry.occupancy(
                                key, "streaming", by_key=True
                            ),
                            "last_error": registry.last_errors.get(
                                (key.name, "streaming")
                            )
                            or registry.last_errors.get((key.name, "batch")),
                            "local_streaming_hours": sum(
                                r["used_hours"]
                                for r in local
                                if r["key_name"] == key.name
                                and r["product"] == "streaming"
                            ),
                            "local_batch_hours": sum(
                                r["used_hours"]
                                for r in local
                                if r["key_name"] == key.name and r["product"] == "batch"
                            ),
                        }
                    )
                    groups[group] = {
                        "name": group,
                        "verified": key.name in registry.verified
                        if provider == "deepgram"
                        else False,
                        "streaming_occupied": registry.occupancy(key, "streaming"),
                        "streaming_limit": registry.limits(key, "streaming"),
                        "batch_occupied": registry.occupancy(key, "batch"),
                        "batch_limit": registry.limits(key, "batch"),
                        **management.get(group, {}),
                    }
                rate_known = (
                    provider == "deepgram"
                    and settings.deepgram_model == "nova-3"
                    and settings.deepgram_language != "multi"
                )
                cost_items = []
                for product, minute_rate in (("streaming", 0.0048), ("batch", 0.0043)):
                    hours = sum(
                        r["used_hours"]
                        for r in local
                        if r["provider"] == provider and r["product"] == product
                    )
                    rate = (
                        (minute_rate + (0.0013 if settings.deepgram_keyterms else 0))
                        * 60
                        if rate_known
                        else None
                    )
                    cost_items.append(
                        {
                            "product": product,
                            "local_hours": hours,
                            "rate_usd_per_hour": rate,
                            "estimated_cost_usd": hours * rate
                            if rate is not None
                            else None,
                            "rate_date": "2026-10-03" if rate_known else None,
                            "rate_source": "https://deepgram.com/pricing"
                            if rate_known
                            else None,
                        }
                    )
                providers.append(
                    {
                        "provider": provider,
                        "configured": bool(configured),
                        "streaming_available": bool(configured)
                        and registry.supports(provider, "streaming")
                        and any(
                            registry.state(k, "streaming") == "healthy"
                            for k in configured
                        ),
                        "batch_available": bool(configured)
                        and registry.supports(provider, "batch")
                        and any(
                            registry.state(k, "batch") == "healthy" for k in configured
                        ),
                        "keys": key_rows,
                        "groups": list(groups.values()),
                        "cost_items": cost_items,
                        "cost_note": "PAYG estimate using current model/language/keyterms; excludes grants, taxes and discounts.",
                        "model": settings.deepgram_model
                        if provider == "deepgram"
                        else settings.speechmatics_model,
                        "streaming_model": settings.deepgram_model
                        if provider == "deepgram"
                        else settings.speechmatics_realtime_model,
                        "usage": sm if provider == "speechmatics" else None,
                    }
                )
        return {
            "order": order,
            "source": source,
            "since": since,
            "until": datetime.now(timezone.utc).isoformat(),
            "providers": providers,
            "usage_note": "Local sent audio and provider project usage are separate; shared usage/balances must not be summed per key.",
        }
